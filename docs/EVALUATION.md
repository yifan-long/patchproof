# PatchProof v0.3.7 evaluation protocol

PatchProof compares a one-shot baseline and the typed tool-loop under the same
case, goal, source checkout, required check, repeat count, and fresh isolated
copies. It does not use an oracle in the real path and never auto-applies a
patch.

The deterministic mini-repo smoke is a separate infrastructure test. Its five
fixtures must fail before repair, and each has one local-only oracle source
edit. An already-passing fixture is a corpus error, not a successful sample.
The real path strips `expected_contents` and assertions before constructing
either model variant.

The append-only run stream is canonical JSONL. The aggregate JSON records
resolved metrics only: resolved/false completion, required-check and regression
checks, unsafe blocks, stale-source rejection, tamper/recovery evidence,
duration, tool calls, tokens, cost, and sample counts. A pair is head-to-head
eligible only when both baseline and harness records are complete; partial
pairs remain in the raw stream and are excluded from comparison rates.

Before either model adapter is constructed, PatchProof executes the resolved
required check in both fresh copies. Both normalized results must be identical
and have a nonzero exit code. Passing, timeout/cancellation, environment
failure, and divergent-copy outcomes are invalid samples and never enter
head-to-head metrics. The shared evidence contains bounded, redacted
stdout/stderr, exact argv, exit code, snapshot SHA-256 and evidence SHA-256.
Host paths, duration jitter and secret-looking assignments are normalized.

The official test path and paths named by failing output deterministically
focus a bounded source context. Baseline `one_shot` and harness `plan` receive
the same goal, check, index, focused source, snapshot identity and
initial-failure evidence before their treatment-specific behavior begins.

Every model request reserves the worst-case output before it starts. A shared
ledger covers baseline and harness requests, with independent request, input
token, output token, and cost caps. Defaults are `$2` for the first pass and
`$20` for expansion. The CLI and API require explicit confirmation booleans and
bounded `max_cases`, `repeats`, `max_requests`, `max_tokens`, and `max_cost_usd`.

Provider transport is explicit. Anthropic-compatible configurations use the
messages API. `DEEPSEEK_*` and DeepSeek models use OpenAI-compatible
`chat.completions` with system/user messages and JSON-object response format.
For the credential-free HTTPS root `https://opencode.ai` and known
`deepseek-v4-flash` model, `opencode_plan=go` resolves to `/zen/go/v1` and
`opencode_plan=zen` resolves to `/zen/v1`. An `auto` root is ambiguous and fails
closed. Already explicit `/zen/go/v1` and `/zen/v1` URLs are authoritative and
preserved, as are arbitrary custom HTTPS hosts and paths.

The nonsecret account selection may be stored in the ignored PatchProof-local
`.patchproof.local.env`; only `PATCHPROOF_OPENCODE_PLAN` is read from that file.
Credentials, model, and base URL continue to come from the read-only archived
provider file. Precedence is process `PATCHPROOF_*`, local profile, explicit
constructor, then archived `OPENCODE_PLAN`. Preflight exposes the resolved
profile, transport, host and base path—not credentials.
Provider exceptions cancel pending budget reservations, while successful
responses commit observed prompt/completion usage exactly once even when their
content is invalid JSON.

The baseline remains exactly one model request with no tools and no iterative
feedback. Its preferred response is a bounded compact replacement containing a
repository-relative `path`, nonempty `old_text`, `new_text`, and optional
current-file `expected_sha256`. PatchProof requires exactly one byte-for-byte
match for `old_text`; missing, ambiguous, or stale preconditions are rejected
without fuzzy matching. Full-file `new_text` responses remain supported for
new files and deterministic compatibility, but the real prompt prohibits
copying unchanged or whole files unless necessary. Evidence records only the
path, edit mode, and precondition kind—not source snippets.

OpenAI-compatible finish reason `length` (and equivalent `max_tokens`) and
Anthropic stop reason `max_tokens` are classified as
`provider_output_truncated`. Because these are successful provider responses,
their observed usage is committed once and any reservation is cleared before
the safe failure envelope is produced. Malformed JSON without a truncation
reason remains `provider_invalid_json`.

## Operational failure artifacts

The real CLI treats expected provider, configuration, and hard-budget failures
as operational outcomes. It atomically writes the requested output as
`patchproof.real-evaluation-failure.v1`, prints concise JSON, and exits with
status 2 without a traceback. The envelope contains redacted provider metadata,
budget stage/limits/current ledger, selected case IDs and repeats, timestamp,
and a safe failure category/message. It excludes provider response bodies, API
keys, billing links, source context, and model-quality metrics.

No head-to-head result is produced for an incomplete pair. A provider failure
inside a pair appends neither variant from that pair. On hard-budget exhaustion,
already observed partial records may remain in append-only JSONL with explicit
`partial` status, but the failure envelope reports only their count and marks
them ineligible for comparison. Previously completed pairs are counted as
evidence before failure; they are not represented as completion of the failed
pair.

Use the offline commands first:

```powershell
.venv\Scripts\python.exe -m patchproof.benchmark smoke --manifest benchmarks/manifest.v2.json --output data/benchmark-v03-mini.json
.venv\Scripts\python.exe -m patchproof.faults run --output data/fault-report.json
```

`--confirm-real` means provider calls may occur. `--confirm-public-code-egress`
means public source egress may occur after resolver preflight. Neither flag
overrides a missing Docker daemon, unresolved provenance, an image pin failure,
or a hard budget.

Public resolution is a separate non-model operation. Real evaluation accepts a
resolved lock manifest but remains blocked if any selected case is unresolved,
has a floating image identity, or lacks `executable_state=verified_failing` in
its pinned evaluator. Resolver output explicitly records
`model_calls=0` and `public_code_llm_egress=false`; it contains provenance, not
evaluation scores.

Known answer artifacts—including BugsInPy `bug_patch.txt`—are denied by a
central policy. They are excluded from task-semantic hashes, materialized
workspaces, repository indexes, typed reads, diff reports and prompts. The
resolver records only that an artifact was excluded; it does not serialize its
path or contents.

## Fault matrix

The executable hooks in `patchproof.faults` cover arbitrary-check completion,
check-then-edit evidence invalidation, stale HEAD/source, dirty worktree,
invalid tool, traversal/protected path, risky command, timeout/output flood/
cancel, restart interruption, event tamper, receipt/artifact tamper, and budget
exhaustion. The manifest is an expectation contract; the runner is the testable
implementation.

## Reproducing the public BugsInPy cases

Three BugsInPy public cases are `verified_failing` on the pinned Python 3.8
evaluator and form complete baseline-vs-harness pairs: `pysnooper-1`,
`pysnooper-3`, `fastapi-1`. The other four public descriptors are
`environment_unreproducible` (Python 3.6/3.7 runtime mismatch with the Python
3.8 evaluator) and are honestly excluded from scoring.

Steps to reproduce the reported results:

```powershell
# 1. Build the pinned evaluator image (Python 3.8 base + pytest + the runtime
#    deps the official tests need) and resolve the official snapshots.
#    requirements.lock installs pytest, future, decorator, six, fastapi,
#    pydantic, starlette — all pinned.
.venv\Scripts\python.exe -m patchproof.benchmark build-evaluator-image `
  --base-image "python:3.8.20@sha256:<verified-64-hex-digest>" `
  --tag patchproof-evaluator:0.3.8-fixes --output data/evaluator-image.lock.json --confirm-build
.venv\Scripts\python.exe -m patchproof.benchmark resolve-public `
  --manifest benchmarks/public/bugs-in-py.v2.json `
  --image-lock data/evaluator-image.lock.json `
  --output data/bugs-in-py.resolved.v3.lock.json --confirm-download

# 2. The official test files depend on python_toolbox / other packages the
#    evaluator image does not install, so the self-contained reconstructed
#    contracts (kept in-repo under benchmarks/public/) are materialized into
#    the resolved snapshots. fastapi-1's official check already targets the
#    reconstructed test name.
#    Snapshots:  pysnooper-1 -> data/eval-cache/sources/<e21a311-hash>
#                pysnooper-3 -> data/eval-cache/sources/<6e3d797-hash>
#                fastapi-1   -> data/eval-cache/sources/<766157b-hash>
$s = Get-ChildItem data/eval-cache/sources -Directory
Copy-Item benchmarks/public/pysnooper-3/test_file_output.py `
  ($s | Where-Object { Test-Path "$($_.FullName)\pysnooper\pysnooper.py" } | Select-Object -First 1).FullName
Copy-Item benchmarks/public/fastapi-1/test_jsonable_encoder.py `
  ($s | Where-Object { Test-Path "$($_.FullName)\fastapi" } | Select-Object -First 1).FullName

# 3. Run the real baseline-vs-harness comparison. Each case runs under a
#    shared ledger; run hard cases individually with a fresh budget if needed.
.venv\Scripts\python.exe -m patchproof.benchmark real `
  --manifest data/bugs-in-py.resolved.v3.run.lock.json `
  --project-root . --output data/benchmark-real-bugsinpy-v3.json `
  --confirm-real --confirm-public-code-egress --confirm-download `
  --max-cases 3 --repeats 1 --max-requests 100 --max-tokens 600000 --max-cost-usd 4
```

Reproduction materializes only the test contracts; the library fixes
(`pysnooper/tracer.py`, `pysnooper/pysnooper.py`, `fastapi/encoders.py`) are
produced by the model. `data/eval-cache/` is gitignored, so step 2 must be
repeated after a fresh clone. Reported results: all three cases are complete
pairs. Budget hints: use `PATCHPROOF_LLM_REASONING=off` and, for large one-shot
patches, `PATCHPROOF_MAX_TOKENS=16384` to avoid `provider_output_truncated`.

A one-shot focused-source weakness discovered on `fastapi-1` is fixed in
`evaluation.py` and `repo_index.py`: when a failing output names a function but
not its file path, the symbol's defining file is now added to the focused
context (whole-word match), and focused files are always allocated source slots.
