# Benchmark Harness (v0.3.7)

PatchProof has two deliberately different evaluation modes.

Deterministic smoke is **infrastructure validation**, not a model-quality
comparison. Every mini fixture is intentionally broken. Before creating any
result, smoke runs the exact required check and rejects the corpus if a fixture
already passes. Each case contains exactly one local-only full-file oracle edit.
The baseline applies it through the snapshot workspace; the FakeLLM harness
must execute exactly `apply_edit`, `run_check`, then `finish`. Both variants must
pass after repair, report the expected changed file, and produce a nonempty
patch. Stable v0.3.2 expectations are 10 successful runs, one changed file per
run, nonzero patch sizes, and three harness tool calls per case. Durations and
byte sizes remain observed values.

The optional real mode compares the same case, same goal and same
`check_command` on two fresh isolated copies:

- `baseline_one_shot_real`: exactly one real model call returns bounded compact
  exact replacements by default (full-file edits remain a compatibility mode);
  the result is checked in its copy and is never written to the source repo.
- `harness_tool_loop_real`: real plan/action/observation calls use the typed
  loop, policy gates, edit preconditions, repair budget and Patch Receipt; it
  also never calls Apply.

Real mode receives an oracle-stripped case view and never reads
`expected_contents`; that field is only a deterministic fixture oracle. It
requires an explicit case limit and estimated cost cap.
The cost estimate is conservative when token accounting is unavailable and is
not a provider invoice.

Compact edits require nonempty `old_text` with exactly one match in the current
file. An optional SHA-256 adds a byte-exact current-file precondition. Missing,
ambiguous, stale, out-of-policy, oversized, or excessive edits are rejected;
no fuzzy matching or source snippets appear in evidence. Provider token-limit
finish reasons produce `provider_output_truncated`, not `provider_invalid_json`,
and cannot produce a partial head-to-head claim.

The versioned corpus manifest is `benchmarks/manifest.v2.json`. It contains five
local cases with strict `BenchmarkCase` v2 fields: source kind, argv setup and
required check, image/resource policy, allowed/expected paths, repeats and
provenance. Public unresolved descriptors are kept separately in
`benchmarks/public/bugs-in-py.v2.json`.

```powershell
cd C:\Users\Administrator\Desktop\简历项目\patchproof
.venv\Scripts\python.exe -m patchproof.benchmark smoke `
  --manifest benchmarks/manifest.v2.json `
  --project-root . `
  --output data/benchmark-smoke.json
```

The optional real-model entry point requires explicit `--confirm-real` and
`--confirm-public-code-egress` for public cases. It also requires Docker
preflight and resolved immutable public provenance. Provider calls and cost are
real; defaults are a `$2` first pass and `$20` expansion budget. Keep all caps
explicit in reproducible commands.

```powershell
.venv\Scripts\python.exe -m patchproof.benchmark real `
  --manifest benchmarks/manifest.v2.json `
  --project-root . `
  --output data/benchmark-real.json `
  --confirm-real --confirm-public-code-egress --max-cases 1 `
  --repeats 1 --max-requests 40 --max-tokens 32768 --max-cost-usd 2
```

## Report template

Numbers below are intentionally blank until a reproducible run is performed.

| variant | success rate | mean steps | mean tool calls | mean duration | mean changed files | mean patch size | approvals | required-check | receipt file | event chain | precondition |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| baseline_one_shot_real | — | — | — | — | — | — | — | n/a | n/a | n/a | — |
| harness_tool_loop_real | — | — | — | — | — | — | — | — | — | — | — |

Reports additionally retain the exact `goal`, `check_command`, changed paths,
failure category, command exit code, model usage, approval count, logical
receipt verification, receipt artifact file verification and event-chain
verification. No numbers in this template are claimed results.
