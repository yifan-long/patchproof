# Windows local demo

`demo.cmd` is the Windows double-click entry point for a complete local PatchProof demo. It prepares dependencies, starts the API and UI in the background, waits for both services to become healthy, then opens `http://localhost:5175`.

## Prerequisites

- Windows 10 or newer.
- [uv](https://docs.astral.sh/uv/) on `PATH`.
- Node.js and pnpm on `PATH`.

The launcher does not install system software. On every start it safely runs `uv sync` and `pnpm install --frozen-lockfile`; both commands are lockfile-driven and idempotent.

## First run

Double-click `demo.cmd`, or run:

```powershell
.\demo.cmd
```

The first run asks for provider base URL, model, transport and API key. The key prompt is masked. The launcher then binds the backend to `127.0.0.1:8010` and the frontend to `127.0.0.1:5175` without uvicorn reload.

For an automated or scripted setup, keep the key out of command history by reading it as a `SecureString`:

```powershell
$key = Read-Host 'API key' -AsSecureString
.\deploy\local-demo.ps1 configure `
  -BaseUrl 'https://provider.example/v1' `
  -Model 'your-model' `
  -Transport 'openai-compatible' `
  -ApiKey $key
.\deploy\local-demo.ps1 start -NoBrowser
```

Do not write a literal API key into a command, script, screenshot or issue report.

## Commands

| Command | Purpose |
|---|---|
| `.\demo.cmd` or `.\demo.cmd start` | Prepare and start; open the browser after health checks |
| `.\demo.cmd configure` | Replace provider configuration and encrypted key |
| `.\demo.cmd status` | Show verified backend/frontend process state |
| `.\demo.cmd logs` | Print the last 80 lines of each local log |
| `.\demo.cmd stop` | Stop verified backend/frontend process trees |
| `.\deploy\local-demo.ps1 start -NoBrowser` | Start without opening a browser, useful for automation |

Run `stop` before `configure` when changing the provider used by an already-running backend, then start again.

## Secret and process safety

- `deploy/.local-demo.config.json` stores base URL, model and transport in plain text. Its API key field is encrypted directly with Windows DPAPI `CurrentUser` and stored as Base64 ciphertext, binding it to the current Windows user while remaining interoperable between Windows PowerShell 5.1 and PowerShell 7.
- The config and all runtime state are Git-ignored. DPAPI encryption is not portable to another Windows user and does not protect against a compromise of the same logged-in account.
- On start, the key is decrypted only long enough to place it in the backend child process environment. The parent environment is restored immediately; the frontend is started afterwards and never receives the key.
- The UI persists only non-secret provider fields. It discards an `api_key` left by an older PatchProof version instead of restoring it from `localStorage`.
- Logs, PIDs and process metadata live under `deploy/.local-demo/`. The state file never contains provider credentials.
- `stop` validates PID, executable path and precise process start time before calling `taskkill /T`. A reused or stale PID is reported and skipped.

The key necessarily exists in backend process memory/environment while the demo is running so the backend can create model clients. Do not run the demo under a shared or untrusted Windows account.

## Troubleshooting

**A required tool is missing.** Install the named tool yourself, reopen the terminal so `PATH` is refreshed, and retry. The launcher never performs system-level installation.

**Port 8010 or 5175 is occupied.** The launcher refuses to take over untracked listeners. Stop the conflicting application, then run `start` again.

**The config cannot be decrypted.** Run `configure` as the Windows user who will run the demo. Copied config files cannot be decrypted under another user context.

**A service exits during startup.** Run `.\demo.cmd logs`. Dependency output remains in the launch console; service stdout/stderr is stored under `deploy/.local-demo/`.

**Status says degraded or stale.** Run `.\demo.cmd stop`; identity-matched processes are stopped and mismatched PIDs are left alone. Then start again.

This launcher is for a local demonstration. The Linux Docker + Caddy deployment remains documented in [DEPLOYMENT.md](DEPLOYMENT.md).
