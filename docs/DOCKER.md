# Docker evaluation isolation

`DockerEvalExecutor` is an injectable execution layer. Tests assert its exact
argv with a fake runner; production execution is separate from build/setup
networking and cannot silently fall back to the local process executor.

Execution containers use:

- a digest-pinned image;
- `--read-only` rootfs and one isolated writable `/workspace` bind mount;
- `--network none`, deterministic `TZ`, locale and `PYTHONHASHSEED`;
- CPU, memory, PID, timeout, cancellation and output limits;
- `no-new-privileges`, `--cap-drop ALL`, no privileged mode and no Docker socket.

Build/setup commands use a separately configured explicit network mode. They
produce setup evidence and do not change the execution command's `none`
network. The daemon is never reconfigured automatically.

Preflight reports CLI availability, daemon/version, image pin and availability,
cache state, explicit registry/ACR host, package-mirror host, and execution mode without secrets. A
`local://patchproof-python312` marker means `local_smoke_only`; it is not a
Docker isolation claim.

Domestic mirror strategy is configuration-only: an explicit registry/ACR image
mapping may be supplied for pinned images, while TUNA mirrors are limited to
apt/pip/package setup commands. PatchProof never mutates Docker daemon config.

The controlled evaluator image is built only by an explicit command with a
digest-pinned base image:

```powershell
.venv\Scripts\python.exe -m patchproof.benchmark build-evaluator-image `
  --base-image python@sha256:<verified-64-hex-digest> `
  --output data/evaluator-image.lock.json `
  --confirm-build
```

The builder uses `--pull=false`, never requests privileged mode or mounts the
Docker socket, and inspects the resulting image. Its checksummed lock records
the immutable local `sha256:` image ID, repository digests, Dockerfile and
dependency-lock hashes, and safe build policy. A direct Docker image ID is an
explicit immutable reference accepted by the evaluator.

The build also probes `platform.python_version()` inside the completed image
with a read-only root filesystem and `--network none`; the verified runtime is
written to the image lock. Public resolution compares its major/minor version
with BugsInPy metadata before running the official check. A mismatch is
`runtime_version_mismatch`, not a resolved executable case. PatchProof never
falls back to the host interpreter or silently pulls an old floating image.

`--acr-registry` changes only the local image name. TUNA is accepted only as a
package mirror, for example
`--pip-index-url https://pypi.tuna.tsinghua.edu.cn/simple`; neither option logs
into a registry or alters daemon configuration.

When the daemon is unavailable, public and real evaluation is reported as
blocked. The offline mini-repo smoke and fault hooks remain runnable as local
smoke with the label above.
