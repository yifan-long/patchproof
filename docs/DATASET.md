# PatchProof v0.3.7 dataset and provenance

The local corpus contains five self-contained, intentionally failing
PatchProof-owned mini repos under `benchmarks/fixtures/`. They require no
network. Each manifest entry has exactly one fixed full-file
`expected_contents` oracle whose path exactly matches `expected_changed_files`:

| case | semantic focus |
| --- | --- |
| `mini-validation` | validation and normalization |
| `mini-config-precedence` | explicit env/file/default precedence |
| `mini-pagination` | page boundary behavior |
| `mini-idempotency` | idempotent state transitions |
| `mini-serialization` | backward-compatible serialization |

Oracle fields are rejected for non-local cases. Deterministic smoke records the
failing required check, then uses the oracle only for the local baseline and
FakeLLM infrastructure path. Real/public evaluation receives a case copy with
oracle fields removed.

Public descriptors are in `benchmarks/public/bugs-in-py.v2.json`. They use
official BugsInPy identifiers and official metadata URLs only:

| project | bug | official identifier |
| --- | ---: | --- |
| youtube-dl | 2 | `bugsinpy-checkout -p youtube-dl -v 0 -i 2` |
| PySnooper | 1 | official BugsInPy project/bug tree |
| PySnooper | 3 | official BugsInPy project/bug tree |
| fastapi | 1 | official BugsInPy project/bug tree |
| black | 1 | official BugsInPy project/bug tree |
| cookiecutter | 1 | official BugsInPy project/bug tree |
| httpie | 1 | resolver-gated official project/bug tree |

All five source descriptors have `provenance_state=unresolved`. They intentionally
do not claim an upstream commit, source license, or image digest. A resolver
must obtain and verify those values, verify checkout HEAD, resolve an SPDX
license and immutable image digest, and record machine-readable evidence before
public evaluation can proceed. `resolve-public` writes a separate canonical
`patchproof.public-lock.v1` manifest; it never edits the descriptor. Third-party
source is cached by dataset URL/revision or source URL/commit under the ignored
`data/eval-cache/`; it is never vendored.

Resolution is explicit and does not invoke a model or send public code to an
LLM:

```powershell
.venv\Scripts\python.exe -m patchproof.benchmark resolve-public `
  --manifest benchmarks/public/bugs-in-py.v2.json `
  --image-lock data/evaluator-image.lock.json `
  --output data/bugs-in-py.resolved.lock.json `
  --confirm-download
```

An already verified official dataset checkout can be replayed without network
using `--dataset-root` together with its full `--dataset-revision`; source
caches still undergo detached-HEAD verification. These options do not weaken
the explicit confirmation, image-runtime, or failing-check requirements.

The resolver pins the official BugsInPy dataset revision, reads its
`project.info` and `bug.info`, verifies the exact buggy checkout HEAD, hashes
metadata and checked-out content, and conservatively identifies license
evidence. Missing, conflicting, unsafe, or irreproducible evidence produces a
structured unresolved reason. The lock records the fixed commit only as
official task identity; it is never exposed as a repair oracle.

For task semantics, v0.3.7 requires `python_version`, `test_file`, and
`run_test.sh` from the official bug directory. `run_test.sh` must contain
exactly one nonempty command line that tokenizes to an approved argv without a
shell executable, composition, redirection, or environment assignment. The
resolved goal names only the official project/bug and failing test identity.
The lock records SHA-256 evidence for `project.info`, `bug.info`, and
`run_test.sh`; it never derives semantics from `bug_patch.txt`.

A public case becomes resolved only when dataset metadata, full buggy and fixed
commits, checked-out HEAD, license/SPDX evidence, and an evaluator image lock
are all verifiable. It must also reproduce a nonzero official check in an image
whose probed Python major/minor matches official metadata. Passing tests,
missing dependencies, unsafe commands, runtime mismatches, or unavailable
Docker probes remain unresolved. `expected_contents` and assertions are always
empty in the public lock.

`bug_patch.txt` and known patch/fix/oracle variants are excluded without
reading their contents. They are not copied into probe/evaluation workspaces
and cannot be read through PatchProof tools.

The official dataset attribution and metadata source are the BugsInPy GitHub
repository and README linked in the manifest. Tests use local fake official
metadata and local Git repositories; preflight never downloads source.
