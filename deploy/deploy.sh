#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_PATH="${BASH_SOURCE[0]}"
[[ "$SCRIPT_PATH" == */* ]] || SCRIPT_PATH="./$SCRIPT_PATH"
SCRIPT_DIR="$(cd -- "${SCRIPT_PATH%/*}" && pwd)"
COMPOSE_FILE="$SCRIPT_DIR/docker-compose.yml"
ENV_FILE="$SCRIPT_DIR/.env"
DATA_DIR="$SCRIPT_DIR/data"
REPOSITORIES_DIR="$SCRIPT_DIR/repositories"
declare -a COMPOSE=()

usage() {
  cat <<'EOF'
PatchProof self-hosted deployment

Usage:
  bash deploy/deploy.sh <domain>       Build and start with automatic HTTPS
  bash deploy/deploy.sh --localhost    Build and start on http://localhost
  bash deploy/deploy.sh upgrade        Rebuild and restart using saved config
  bash deploy/deploy.sh status         Show service and health status
  bash deploy/deploy.sh logs           Follow service logs
  bash deploy/deploy.sh uninstall      Remove containers (persistent data stays)

The script requires Docker Engine with the Docker Compose v2 plugin. It never
installs Docker and never reads or writes an LLM API key.
EOF
}

die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

on_error() {
  local exit_code="$?"
  local line="$1"
  trap - ERR
  printf '\nPatchProof deployment failed at line %s (exit %s).\n' "$line" "$exit_code" >&2
  if ((${#COMPOSE[@]})); then
    "${COMPOSE[@]}" ps >&2 || true
    printf 'Inspect details with: bash deploy/deploy.sh logs\n' >&2
  fi
  exit "$exit_code"
}
trap 'on_error "$LINENO"' ERR

require_docker() {
  command -v docker >/dev/null 2>&1 || die "Docker is not installed; install Docker Engine and Compose v2 first"
  docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is unavailable (expected: docker compose)"
  docker info >/dev/null 2>&1 || die "cannot reach the Docker daemon; start it and ensure this user has permission"
  COMPOSE=(docker compose --project-directory "$SCRIPT_DIR" --env-file "$ENV_FILE" -f "$COMPOSE_FILE")
}

validate_domain() {
  local domain="$1"
  local label

  [[ "$domain" != *://* ]] || die "pass only a domain name, without http:// or https://"
  [[ "$domain" != */* && "$domain" != *:* ]] || die "domain must not contain a path or port"
  ((${#domain} <= 253)) || die "domain is longer than 253 characters"
  [[ "$domain" == *.* ]] || die "a public domain must contain a dot; use --localhost for local smoke"
  [[ "$domain" != .* && "$domain" != *. && "$domain" != *..* ]] || die "invalid domain: $domain"
  IFS='.' read -r -a labels <<<"$domain"
  for label in "${labels[@]}"; do
    ((${#label} >= 1 && ${#label} <= 63)) || die "invalid domain label length in: $domain"
    [[ "$label" =~ ^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?$ ]] || die "invalid domain label '$label'"
  done
}

write_config() {
  local site_address="$1"
  local origin="$2"
  local bind_prefix="$3"
  local tmp

  umask 077
  tmp="$(mktemp "$SCRIPT_DIR/.env.tmp.XXXXXX")"
  printf 'PATCHPROOF_SITE_ADDRESS=%s\nPATCHPROOF_ORIGIN=%s\n' "$site_address" "$origin" >"$tmp"
  printf 'PATCHPROOF_HTTP_BIND=%s80:80\n' "$bind_prefix" >>"$tmp"
  printf 'PATCHPROOF_HTTPS_BIND=%s443:443\n' "$bind_prefix" >>"$tmp"
  printf 'PATCHPROOF_HTTPS_UDP_BIND=%s443:443/udp\n' "$bind_prefix" >>"$tmp"
  mv -f -- "$tmp" "$ENV_FILE"
}

require_config() {
  [[ -f "$ENV_FILE" ]] || die "deployment config is missing; first run with a domain or --localhost"
  grep -q '^PATCHPROOF_SITE_ADDRESS=' "$ENV_FILE" || die "deployment config is invalid; rerun with a domain or --localhost"
  grep -q '^PATCHPROOF_ORIGIN=' "$ENV_FILE" || die "deployment config is invalid; rerun with a domain or --localhost"
  grep -q '^PATCHPROOF_HTTP_BIND=' "$ENV_FILE" || die "deployment config is outdated; rerun with a domain or --localhost"
  grep -q '^PATCHPROOF_HTTPS_BIND=' "$ENV_FILE" || die "deployment config is outdated; rerun with a domain or --localhost"
  grep -q '^PATCHPROOF_HTTPS_UDP_BIND=' "$ENV_FILE" || die "deployment config is outdated; rerun with a domain or --localhost"
}

configured_origin() {
  sed -n 's/^PATCHPROOF_ORIGIN=//p' "$ENV_FILE" | tail -n 1
}

prepare_directories() {
  mkdir -p -- "$DATA_DIR" "$REPOSITORIES_DIR"
}

wait_for_health() {
  local container_id health attempt
  container_id="$("${COMPOSE[@]}" ps -q backend)"
  [[ -n "$container_id" ]] || die "backend container was not created"

  for attempt in {1..60}; do
    health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container_id")"
    case "$health" in
      healthy) return 0 ;;
      unhealthy|exited|dead) die "backend became $health; run 'bash deploy/deploy.sh logs'" ;;
    esac
    sleep 2
  done
  die "backend did not become healthy within 120 seconds; run 'bash deploy/deploy.sh logs'"
}

start() {
  prepare_directories
  "${COMPOSE[@]}" up -d --build --remove-orphans
  wait_for_health
  "${COMPOSE[@]}" ps
  printf '\nPatchProof is running at %s\n' "$(configured_origin)"
  printf 'Repositories placed in %s are visible inside the app as /repositories/<name>.\n' "$REPOSITORIES_DIR"
}

(($# <= 1)) || die "expected one domain or command; see --help"
command_name="${1:-}"
case "$command_name" in
  -h|--help|help)
    usage
    ;;
  --localhost|localhost)
    write_config "http://localhost" "http://localhost" "127.0.0.1:"
    require_docker
    start
    ;;
  upgrade)
    require_config
    require_docker
    start
    ;;
  status)
    require_config
    require_docker
    "${COMPOSE[@]}" ps
    printf 'Configured URL: %s\n' "$(configured_origin)"
    ;;
  logs)
    require_config
    require_docker
    "${COMPOSE[@]}" logs --tail=200 -f backend caddy
    ;;
  uninstall)
    require_config
    require_docker
    "${COMPOSE[@]}" down --remove-orphans
    printf 'Containers removed. Persistent files remain in %s and %s.\n' "$DATA_DIR" "$REPOSITORIES_DIR"
    ;;
  "")
    usage >&2
    exit 2
    ;;
  -* )
    die "unknown option: $command_name"
    ;;
  *)
    validate_domain "$command_name"
    domain="$(printf '%s' "$command_name" | tr '[:upper:]' '[:lower:]')"
    write_config "$domain" "https://$domain" ""
    require_docker
    start
    ;;
esac
