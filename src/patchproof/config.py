from __future__ import annotations

import os
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from dotenv import dotenv_values
from pydantic import Field, PrivateAttr
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PATCHPROOF_ROOT = PROJECT_ROOT / "patchproof"
DEFAULT_ENV_FILE = PROJECT_ROOT / "archive" / "researchflow" / ".env"
DEFAULT_REPO_PATH = PATCHPROOF_ROOT / "benchmarks" / "fixtures" / "validation"
DEFAULT_PROFILE_FILE = PATCHPROOF_ROOT / ".patchproof.local.env"
OPENCODE_ZEN_MODELS = frozenset({"deepseek-v4-flash"})


class ProviderConfigurationError(ValueError):
    pass


def resolve_env_file(value: str | os.PathLike[str] | None = None) -> Path:
    """Resolve the read-only provider source without importing its secrets globally."""

    raw = str(value) if value is not None else os.getenv("PATCHPROOF_ENV_FILE", str(DEFAULT_ENV_FILE))
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = (PATCHPROOF_ROOT / path).resolve()
    return path.resolve()


def read_env_file(path: str | os.PathLike[str] | None = None) -> dict[str, str]:
    """Read provider metadata from an env file, never writing or logging secrets."""

    source = resolve_env_file(path)
    if not source.is_file():
        return {}
    values = dotenv_values(source)
    return {str(key): str(value) for key, value in values.items() if key and value is not None}


def read_local_opencode_plan(path: str | os.PathLike[str]) -> str | None:
    """Read only the nonsecret profile key; all other local keys are ignored."""

    source = Path(path).expanduser()
    if not source.is_absolute():
        source = PATCHPROOF_ROOT / source
    if not source.is_file():
        return None
    selected: str | None = None
    for raw_line in source.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = (item.strip() for item in line.split("=", 1))
        if key != "PATCHPROOF_OPENCODE_PLAN":
            continue
        value = value.strip('"\'').lower()
        if selected is not None and selected != value:
            raise ProviderConfigurationError("local OpenCode plan is defined more than once")
        selected = value
    return selected


current_env_file = resolve_env_file()


class Settings(BaseSettings):
    repo_path: str = str(DEFAULT_REPO_PATH)
    env_file_path: str = str(DEFAULT_ENV_FILE)
    profile_file_path: str = str(DEFAULT_PROFILE_FILE)
    api_host: str = "127.0.0.1"
    api_port: int = 8010
    max_iterations: int = 3
    max_tool_steps: int = 32
    max_invalid_actions: int = 3
    max_file_bytes: int = 200_000
    command_timeout_seconds: int = 120
    max_output_chars: int = 12_000
    database_path: str = str(PATCHPROOF_ROOT / "data" / "patchproof.db")
    cleanup_workspaces: bool = False
    allow_git_worktree: bool = True
    allow_project_target: bool = False
    max_llm_calls: int = 40
    llm_provider: Literal["auto", "anthropic", "deepseek", "custom"] = "auto"
    llm_transport: Literal["auto", "anthropic-compatible", "openai-compatible"] = "auto"
    opencode_plan: Literal["auto", "zen", "go"] = "auto"
    llm_reasoning: Literal["auto", "on", "off"] = "auto"
    llm_timeout_seconds: float = Field(default=120.0, gt=0, le=600)
    llm_max_retries: int = Field(default=2, ge=0, le=10)
    anthropic_api_key: str = Field(default="", repr=False)
    anthropic_model: str = "deepseek-chat"
    anthropic_base_url: str | None = None
    anthropic_max_tokens: int = 4096
    model_cost_per_million_tokens: float = 0.0
    evaluation_first_pass_budget_usd: float = 2.0
    evaluation_expansion_budget_usd: float = 20.0
    evaluation_max_requests: int = 40
    evaluation_max_tokens: int = 32_768
    evaluation_reserve_output_tokens: int = 4096
    docker_cli: str = "docker"
    docker_image: str = "local://patchproof-python312"
    docker_registry: str | None = None
    docker_mirror: str | None = None
    docker_cpu_limit: float = 1.0
    docker_memory_limit: str = "1g"
    docker_pids_limit: int = 128
    docker_output_limit: int = 12_000
    docker_timeout_seconds: int = 120
    cors_origins: str = "http://localhost:5173,http://localhost:5174"
    _deepseek_configuration: bool = PrivateAttr(default=False)

    model_config = SettingsConfigDict(
        env_prefix="PATCHPROOF_",
        extra="ignore",
    )

    def __init__(self, **values: object) -> None:
        # pydantic-settings exposes the conventional ``_env_file`` escape
        # hatch. Translate it into our explicit read-only source so DEEPSEEK_*
        # keys in temporary test files use the same precedence rules.
        env_file = values.pop("_env_file", None)
        if env_file is not None and "env_file_path" not in values:
            if isinstance(env_file, (tuple, list)):
                values["env_file_path"] = str(env_file[0]) if env_file else str(DEFAULT_ENV_FILE)
            else:
                values["env_file_path"] = str(env_file)
        super().__init__(**values)

    @property
    def repo_path_resolved(self) -> Path:
        path = Path(self.repo_path)
        if not path.is_absolute():
            path = (PATCHPROOF_ROOT / path).resolve()
        return path.resolve()

    @property
    def database_path_resolved(self) -> Path:
        path = Path(self.database_path)
        if not path.is_absolute():
            path = PATCHPROOF_ROOT / path
        return path.resolve()

    @property
    def cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]

    @property
    def llm_enabled(self) -> bool:
        return bool(self.anthropic_api_key)

    def model_post_init(self, __context) -> None:
        # Process-level PATCHPROOF_* values have the highest precedence. The
        # archive/researchflow env file is read as a provider source only; its
        # key is never copied to disk or included in metadata.
        explicit_source = "env_file_path" in self.model_fields_set
        source = resolve_env_file(self.env_file_path if explicit_source else None)
        self.env_file_path = str(source)
        provider = read_env_file(source)
        selected_inputs: set[str] = set()

        process_plan = os.getenv("PATCHPROOF_OPENCODE_PLAN")
        local_plan = read_local_opencode_plan(self.profile_file_path)
        explicit_plan = self.opencode_plan if "opencode_plan" in self.model_fields_set else None
        archived_plan = provider.get("OPENCODE_PLAN")
        unprefixed_plan = os.getenv("OPENCODE_PLAN")
        selected_plan = process_plan or local_plan or explicit_plan or archived_plan or unprefixed_plan or "auto"
        normalized_plan = str(selected_plan).strip().lower()
        if normalized_plan not in {"auto", "zen", "go"}:
            raise ProviderConfigurationError("OpenCode plan must be one of: auto, zen, go")
        self.opencode_plan = normalized_plan  # type: ignore[assignment]

        def choose(field: str, *names: str, default: object = None) -> object:
            for prefix in ("PATCHPROOF_",):
                for name in names:
                    value = os.getenv(prefix + name)
                    if value is not None:
                        selected_inputs.add(prefix + name)
                        return value
            if field in self.model_fields_set:
                selected_inputs.add(field)
                return getattr(self, field)
            for name in names:
                value = provider.get(name)
                if value is not None:
                    selected_inputs.add(name)
                    return value
            for name in names:
                value = os.getenv(name)
                if value is not None:
                    selected_inputs.add(name)
                    return value
            return getattr(self, field) if default is None else default

        self.anthropic_api_key = str(
            choose(
                "anthropic_api_key",
                "ANTHROPIC_API_KEY",
                "DEEPSEEK_API_KEY",
                "API_KEY",
                default="",
            )
        )
        self.anthropic_model = str(
            choose(
                "anthropic_model",
                "ANTHROPIC_MODEL",
                "DEEPSEEK_MODEL",
                "MODEL",
                default=self.anthropic_model,
            )
        )
        base_url = choose(
            "anthropic_base_url",
            "ANTHROPIC_BASE_URL",
            "DEEPSEEK_BASE_URL",
            "BASE_URL",
            default=None,
        )
        self.anthropic_base_url = str(base_url) if base_url else None

        max_tokens = choose(
            "anthropic_max_tokens",
            "ANTHROPIC_MAX_TOKENS",
            "DEEPSEEK_MAX_TOKENS",
            "MAX_TOKENS",
            default=self.anthropic_max_tokens,
        )
        cost = choose(
            "model_cost_per_million_tokens",
            "MODEL_COST_PER_MILLION_TOKENS",
            "COST_PER_MILLION_TOKENS",
            default=self.model_cost_per_million_tokens,
        )
        try:
            self.anthropic_max_tokens = int(max_tokens)
            self.model_cost_per_million_tokens = float(cost)
        except (TypeError, ValueError) as exc:
            raise ValueError("provider token/cost configuration must be numeric") from exc
        self._deepseek_configuration = any("DEEPSEEK_" in item for item in selected_inputs)

    @property
    def resolved_provider(self) -> str:
        if self.llm_provider != "auto":
            return self.llm_provider
        if self._deepseek_configuration or self.anthropic_model.lower().startswith("deepseek"):
            return "deepseek"
        if self.resolved_transport == "anthropic-compatible":
            return "anthropic"
        return "custom"

    @property
    def resolved_transport(self) -> Literal["anthropic-compatible", "openai-compatible"]:
        if self.llm_transport != "auto":
            return self.llm_transport
        if self.llm_provider == "deepseek":
            return "openai-compatible"
        if self.llm_provider == "anthropic":
            return "anthropic-compatible"
        if self._deepseek_configuration or self.anthropic_model.lower().startswith("deepseek"):
            return "openai-compatible"
        return "anthropic-compatible"

    @property
    def resolved_base_url(self) -> str | None:
        """Resolve an OpenCode root by explicit account profile; preserve paths."""

        if not self.anthropic_base_url:
            return None
        value = self.anthropic_base_url.strip()
        parsed = urlparse(value)
        normalized_path = parsed.path.rstrip("/")
        exact_opencode_host = (
            parsed.scheme == "https"
            and parsed.hostname == "opencode.ai"
            and parsed.port is None
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
        )
        if (
            self.resolved_transport == "openai-compatible"
            and self.anthropic_model in OPENCODE_ZEN_MODELS
            and exact_opencode_host
        ):
            if normalized_path in {"/zen/go/v1", "/zen/v1"}:
                return value
            if normalized_path == "":
                if self.opencode_plan == "go":
                    return "https://opencode.ai/zen/go/v1"
                if self.opencode_plan == "zen":
                    return "https://opencode.ai/zen/v1"
                raise ProviderConfigurationError(
                    "OpenCode root URL is ambiguous; set PATCHPROOF_OPENCODE_PLAN=go or zen"
                )
        return value

    @property
    def resolved_opencode_plan(self) -> Literal["auto", "zen", "go"]:
        if not self.anthropic_base_url:
            return self.opencode_plan
        parsed = urlparse(self.anthropic_base_url.strip())
        if parsed.scheme == "https" and parsed.hostname == "opencode.ai":
            normalized_path = parsed.path.rstrip("/")
            if normalized_path == "/zen/go/v1":
                return "go"
            if normalized_path == "/zen/v1":
                return "zen"
        return self.opencode_plan

    @property
    def provider_metadata(self) -> dict[str, object]:
        """Non-secret provider information safe for API/UI responses."""

        host = None
        resolved_base_url = self.resolved_base_url
        base_path = None
        if resolved_base_url:
            parsed = urlparse(resolved_base_url)
            host = parsed.hostname
            base_path = parsed.path or "/"
        return {
            "provider": self.resolved_provider,
            "profile": self.resolved_opencode_plan,
            "transport": self.resolved_transport,
            "source": "deepseek-compatible" if self.resolved_provider == "deepseek" else "configured",
            "model": self.anthropic_model,
            "base_url_host": host,
            "base_url_path": base_path,
            "base_url_configured": bool(self.anthropic_base_url),
            "api_key_configured": self.llm_enabled,
            "max_tokens": self.anthropic_max_tokens,
            "cost_per_million_tokens": self.model_cost_per_million_tokens,
        }


settings = Settings()
