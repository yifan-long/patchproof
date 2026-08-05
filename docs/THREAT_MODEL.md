# PatchProof Threat Model

## Assets

- The user's source repository and uncommitted edits.
- Provider credentials loaded from the existing environment.
- The claim that a task was tested and is safe to Apply.
- Receipt and event evidence used during review.

## Trust boundaries

1. The model is untrusted input. Its JSON is validated against six typed tools.
2. The staging workspace is isolated from the source repository.
3. Docker execution is the required boundary for public/real evaluation. The
   local process executor is only an explicitly labeled offline smoke path and
   still has the operating system user's permissions.
4. SQLite is durable storage, not a write-once ledger. Hash-chain verification
   detects tampering; it does not prevent a privileged user from editing the DB.
5. The human is the final authority for risky commands and Apply.

## Safety invariants

- No `shell=True`; execution uses an argv list and `shell=False`.
- Docker execution rejects floating images, privileged mode, Docker socket
  mounts and network access; missing daemon state blocks public/real runs.
- Shell composition, network access, installation, deletion and Git writes are
  not auto-approved.
- A typed edit must provide `expected_sha256` or `old_text`; paths are resolved
  inside the staging root and sensitive files are blocked.
- Compact one-shot edits additionally require a nonempty, unique, exact
  `old_text` match. Optional hashes are checked against current file bytes;
  writes are atomic and bounded, with no fuzzy matching.
- BugsInPy patch/fix/oracle artifacts are denied from source context,
  materialized evaluation copies, typed reads, prompts and reports. Resolver
  task semantics use only conservatively parsed official metadata and one safe
  `run_test.sh` command.
- Public pairs require matching nonzero fail-before evidence from both pinned
  Docker copies before model construction. Passing, inconsistent, unrunnable,
  runtime-incompatible or unverified checks are excluded from scoring.
- `check_command` is normalized once. Only a successful exact-argv required
  check from the current edit generation can authorize `finish(verified)`;
  later edits invalidate that evidence.
- Apply checks the original HEAD/status/manifest against the recorded baseline.
- Deletions are not silently written back.
- A process restart marks running tasks `interrupted` and adds an event.
- A successful test alone is not a completion claim: `finish(verified)` and a
  Patch Receipt are required before `awaiting_apply`.
- The canonical receipt artifact is atomically written and its actual file
  bytes are recorded in SQLite; logical hash or file tampering is detectable.
- Deterministic mini-repo smoke requires a recorded fail-before state and uses
  exactly one local fixture edit only for infrastructure validation. Real
  evaluation receives oracle-stripped cases and uses model-produced edits for both variants, with
  explicit confirmation, shared hard budgets, immutable provenance and no
  auto-Apply. Partial pairs are retained but excluded from head-to-head rates.

## Explicit limitation

The local process executor is not Docker, a VM, or a kernel-level sandbox. A
human should only approve commands they understand, and production deployment
credentials should not be available in the PatchProof process environment.
Docker daemon/image readiness and public provenance are external preflight
requirements; this workspace does not claim them when unavailable.
