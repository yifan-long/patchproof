[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('start', 'configure', 'status', 'logs', 'stop')]
    [string]$Action = 'start',

    [string]$BaseUrl,
    [string]$Model,

    [ValidateSet('auto', 'anthropic-compatible', 'openai-compatible')]
    [string]$Transport,

    [System.Security.SecureString]$ApiKey,
    [switch]$NoBrowser
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if ($env:OS -eq 'Windows_NT' -and -not ('System.Security.Cryptography.ProtectedData' -as [type])) {
    try {
        Add-Type -AssemblyName System.Security -ErrorAction Stop
    }
    catch {
        Add-Type -AssemblyName System.Security.Cryptography.ProtectedData -ErrorAction Stop
    }
}

$ScriptDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDirectory
$ConfigPath = Join-Path $ScriptDirectory '.local-demo.config.json'
$RuntimeDirectory = Join-Path $ScriptDirectory '.local-demo'
$StatePath = Join-Path $RuntimeDirectory 'state.json'
$BackendOutLog = Join-Path $RuntimeDirectory 'backend.out.log'
$BackendErrLog = Join-Path $RuntimeDirectory 'backend.err.log'
$FrontendOutLog = Join-Path $RuntimeDirectory 'frontend.out.log'
$FrontendErrLog = Join-Path $RuntimeDirectory 'frontend.err.log'
$BackendUrl = 'http://127.0.0.1:8010/health'
$FrontendUrl = 'http://localhost:5175'

function Write-JsonFile {
    param(
        [Parameter(Mandatory = $true)] [string]$Path,
        [Parameter(Mandatory = $true)] [object]$Value
    )

    $directory = Split-Path -Parent $Path
    if (-not (Test-Path -LiteralPath $directory)) {
        New-Item -ItemType Directory -Path $directory -Force | Out-Null
    }
    $temporaryPath = "$Path.tmp"
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($temporaryPath, ($Value | ConvertTo-Json -Depth 6), $encoding)
    Move-Item -LiteralPath $temporaryPath -Destination $Path -Force
}

function Protect-ApiKeyForCurrentUser {
    param([Parameter(Mandatory = $true)] [System.Security.SecureString]$Value)

    $bstr = [IntPtr]::Zero
    $plainBytes = $null
    $protectedBytes = $null
    $plainText = $null
    try {
        $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Value)
        $plainText = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
        $plainBytes = [Text.Encoding]::UTF8.GetBytes($plainText)
        $protectedBytes = [System.Security.Cryptography.ProtectedData]::Protect(
            $plainBytes,
            $null,
            [System.Security.Cryptography.DataProtectionScope]::CurrentUser
        )
        return [Convert]::ToBase64String($protectedBytes)
    }
    finally {
        if ($plainBytes) { [Array]::Clear($plainBytes, 0, $plainBytes.Length) }
        if ($protectedBytes) { [Array]::Clear($protectedBytes, 0, $protectedBytes.Length) }
        if ($bstr -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
        $plainText = $null
    }
}

function Unprotect-ApiKeyForCurrentUser {
    param([Parameter(Mandatory = $true)] [string]$Value)

    $protectedBytes = $null
    $plainBytes = $null
    try {
        $protectedBytes = [Convert]::FromBase64String($Value)
        $plainBytes = [System.Security.Cryptography.ProtectedData]::Unprotect(
            $protectedBytes,
            $null,
            [System.Security.Cryptography.DataProtectionScope]::CurrentUser
        )
        return [Text.Encoding]::UTF8.GetString($plainBytes)
    }
    finally {
        if ($plainBytes) { [Array]::Clear($plainBytes, 0, $plainBytes.Length) }
        if ($protectedBytes) { [Array]::Clear($protectedBytes, 0, $protectedBytes.Length) }
    }
}

function Read-Config {
    if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
        return $null
    }
    try {
        $config = Get-Content -Raw -Encoding UTF8 -LiteralPath $ConfigPath | ConvertFrom-Json
    }
    catch {
        throw "Local demo config is invalid. Run '.\demo.cmd configure' to replace it."
    }
    foreach ($property in @('base_url', 'model', 'transport', 'api_key_dpapi')) {
        if (-not $config.PSObject.Properties[$property] -or [string]::IsNullOrWhiteSpace([string]$config.$property)) {
            throw "Local demo config is incomplete. Run '.\demo.cmd configure' to replace it."
        }
    }
    if ($config.transport -notin @('auto', 'anthropic-compatible', 'openai-compatible')) {
        throw "Local demo transport is invalid. Run '.\demo.cmd configure' to replace it."
    }
    return $config
}

function Read-RequiredValue {
    param(
        [Parameter(Mandatory = $true)] [string]$Label,
        [string]$CurrentValue
    )

    $suffix = if ([string]::IsNullOrWhiteSpace($CurrentValue)) { '' } else { " [$CurrentValue]" }
    $value = Read-Host "$Label$suffix"
    if ([string]::IsNullOrWhiteSpace($value)) {
        $value = $CurrentValue
    }
    if ([string]::IsNullOrWhiteSpace($value)) {
        throw "$Label is required."
    }
    return $value.Trim()
}

function Save-Configuration {
    $existing = Read-Config
    $selectedBaseUrl = $BaseUrl
    $selectedModel = $Model
    $selectedTransport = $Transport
    $selectedApiKey = $ApiKey

    if ([string]::IsNullOrWhiteSpace($selectedBaseUrl)) {
        $selectedBaseUrl = Read-RequiredValue -Label 'Provider base URL' -CurrentValue $(if ($existing) { $existing.base_url } else { $null })
    }
    if ([string]::IsNullOrWhiteSpace($selectedModel)) {
        $selectedModel = Read-RequiredValue -Label 'Model' -CurrentValue $(if ($existing) { $existing.model } else { $null })
    }
    if ([string]::IsNullOrWhiteSpace($selectedTransport)) {
        $defaultTransport = if ($existing) { [string]$existing.transport } else { 'auto' }
        $selectedTransport = Read-RequiredValue -Label 'Transport (auto / anthropic-compatible / openai-compatible)' -CurrentValue $defaultTransport
    }
    if ($selectedTransport -notin @('auto', 'anthropic-compatible', 'openai-compatible')) {
        throw 'Transport must be auto, anthropic-compatible, or openai-compatible.'
    }

    $parsedBaseUrl = $null
    if (-not [System.Uri]::TryCreate($selectedBaseUrl, [System.UriKind]::Absolute, [ref]$parsedBaseUrl) -or
        $parsedBaseUrl.Scheme -notin @('http', 'https')) {
        throw 'Provider base URL must be an absolute http:// or https:// URL.'
    }

    if ($null -eq $selectedApiKey) {
        $selectedApiKey = Read-Host 'API key (encrypted for the current Windows user)' -AsSecureString
    }
    if ($null -eq $selectedApiKey -or $selectedApiKey.Length -eq 0) {
        throw 'API key is required.'
    }

    $encryptedApiKey = Protect-ApiKeyForCurrentUser -Value $selectedApiKey
    $config = [ordered]@{
        schema_version = 2
        base_url = $selectedBaseUrl.Trim()
        model = $selectedModel.Trim()
        transport = $selectedTransport
        api_key_dpapi = $encryptedApiKey
        updated_at_utc = [DateTime]::UtcNow.ToString('o')
    }
    Write-JsonFile -Path $ConfigPath -Value $config
    Write-Host "Configuration saved for Windows user '$env:USERNAME'."
    Write-Host 'The API key is DPAPI-encrypted and was not printed.'
}

function Get-RequiredCommand {
    param(
        [Parameter(Mandatory = $true)] [string[]]$Names,
        [Parameter(Mandatory = $true)] [string]$InstallHint
    )

    foreach ($name in $Names) {
        $command = Get-Command $name -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($command) {
            return $command
        }
    }
    throw "Missing required tool '$($Names[0])'. $InstallHint"
}

function Invoke-NativeChecked {
    param(
        [Parameter(Mandatory = $true)] [string]$Executable,
        [Parameter(Mandatory = $true)] [string[]]$Arguments,
        [Parameter(Mandatory = $true)] [string]$FailureMessage
    )

    & $Executable @Arguments | ForEach-Object { Write-Host $_ }
    if ($LASTEXITCODE -ne 0) {
        throw "$FailureMessage (exit $LASTEXITCODE)."
    }
}

function Prepare-Dependencies {
    $uv = Get-RequiredCommand -Names @('uv.exe', 'uv') -InstallHint 'Install uv, reopen the terminal, then retry.'
    $pnpm = Get-RequiredCommand -Names @('pnpm.cmd', 'pnpm') -InstallHint 'Install Node.js and pnpm, reopen the terminal, then retry.'
    $node = Get-RequiredCommand -Names @('node.exe', 'node') -InstallHint 'Install Node.js, reopen the terminal, then retry.'

    Write-Host 'Preparing Python dependencies (uv sync)...'
    $previousUvCache = [Environment]::GetEnvironmentVariable('UV_CACHE_DIR', 'Process')
    [Environment]::SetEnvironmentVariable('UV_CACHE_DIR', (Join-Path $ProjectRoot '.uv-cache'), 'Process')
    Push-Location $ProjectRoot
    try {
        Invoke-NativeChecked -Executable $uv.Source -Arguments @('sync') -FailureMessage 'uv sync failed'
    }
    finally {
        Pop-Location
        [Environment]::SetEnvironmentVariable('UV_CACHE_DIR', $previousUvCache, 'Process')
    }

    Write-Host 'Preparing frontend dependencies (pnpm install --frozen-lockfile)...'
    Push-Location (Join-Path $ProjectRoot 'frontend')
    try {
        Invoke-NativeChecked -Executable $pnpm.Source -Arguments @('install', '--frozen-lockfile') -FailureMessage 'pnpm install failed'
    }
    finally {
        Pop-Location
    }

    $pythonPath = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
    $vitePath = Join-Path $ProjectRoot 'frontend\node_modules\vite\bin\vite.js'
    if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
        throw "uv sync completed but '$pythonPath' is missing."
    }
    if (-not (Test-Path -LiteralPath $vitePath -PathType Leaf)) {
        throw "pnpm install completed but '$vitePath' is missing."
    }
    return [pscustomobject]@{
        Python = (Resolve-Path -LiteralPath $pythonPath).Path
        Node = $node.Source
        Vite = (Resolve-Path -LiteralPath $vitePath).Path
    }
}

function Test-PortAvailable {
    param([Parameter(Mandatory = $true)] [int]$Port)

    $listener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, $Port)
    try {
        $listener.Start()
        return $true
    }
    catch {
        return $false
    }
    finally {
        $listener.Stop()
    }
}

function New-ProcessIdentity {
    param(
        [Parameter(Mandatory = $true)] [System.Diagnostics.Process]$Process,
        [Parameter(Mandatory = $true)] [string]$ExecutablePath
    )

    $Process.Refresh()
    return [ordered]@{
        pid = $Process.Id
        executable_path = [System.IO.Path]::GetFullPath($ExecutablePath)
        start_time_filetime_utc = $Process.StartTime.ToUniversalTime().ToFileTimeUtc()
    }
}

function Test-RecordedProcess {
    param([Parameter(Mandatory = $true)] [object]$Identity)

    try {
        $process = Get-Process -Id ([int]$Identity.pid) -ErrorAction Stop
        $actualPath = [System.IO.Path]::GetFullPath($process.Path)
        $expectedPath = [System.IO.Path]::GetFullPath([string]$Identity.executable_path)
        $samePath = $actualPath.Equals($expectedPath, [System.StringComparison]::OrdinalIgnoreCase)
        $sameStart = $process.StartTime.ToUniversalTime().ToFileTimeUtc() -eq [long]$Identity.start_time_filetime_utc
        return $samePath -and $sameStart
    }
    catch {
        return $false
    }
}

function Read-State {
    if (-not (Test-Path -LiteralPath $StatePath -PathType Leaf)) {
        return $null
    }
    try {
        return Get-Content -Raw -Encoding UTF8 -LiteralPath $StatePath | ConvertFrom-Json
    }
    catch {
        throw "Local demo state is invalid: '$StatePath'."
    }
}

function Get-CurrentStatus {
    $state = Read-State
    if (-not $state) {
        return [pscustomobject]@{ Name = 'stopped'; State = $null; Backend = $false; Frontend = $false }
    }
    $backendRunning = Test-RecordedProcess -Identity $state.backend
    $frontendRunning = Test-RecordedProcess -Identity $state.frontend
    $name = if ($backendRunning -and $frontendRunning) { 'running' } elseif ($backendRunning -or $frontendRunning) { 'degraded' } else { 'stopped' }
    return [pscustomobject]@{ Name = $name; State = $state; Backend = $backendRunning; Frontend = $frontendRunning }
}

function Stop-VerifiedProcessTree {
    param(
        [Parameter(Mandatory = $true)] [string]$Role,
        [Parameter(Mandatory = $true)] [object]$Identity
    )

    if (-not (Test-RecordedProcess -Identity $Identity)) {
        Write-Host "$Role PID $($Identity.pid) is absent or no longer matches its recorded identity; skipped."
        return
    }
    & "$env:SystemRoot\System32\taskkill.exe" /PID ([string]$Identity.pid) /T /F 2>$null | Out-Null
    if ($LASTEXITCODE -notin @(0, 128)) {
        throw "Could not stop $Role process tree (taskkill exit $LASTEXITCODE)."
    }
    Write-Host "$Role process tree stopped."
}

function Stop-LocalDemo {
    $state = Read-State
    if (-not $state) {
        Write-Host 'PatchProof local demo is not running (no state file).'
        return
    }
    Stop-VerifiedProcessTree -Role 'Frontend' -Identity $state.frontend
    Stop-VerifiedProcessTree -Role 'Backend' -Identity $state.backend
    if (Test-Path -LiteralPath $StatePath) {
        Remove-Item -LiteralPath $StatePath -Force
    }
    Write-Host 'PatchProof local demo is stopped.'
}

function Wait-ForEndpoint {
    param(
        [Parameter(Mandatory = $true)] [string]$Name,
        [Parameter(Mandatory = $true)] [string]$Url,
        [Parameter(Mandatory = $true)] [object]$Identity
    )

    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        if (-not (Test-RecordedProcess -Identity $Identity)) {
            throw "$Name exited before becoming healthy. Inspect '.\demo.cmd logs'."
        }
        try {
            $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 2
            if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 400) {
                return
            }
        }
        catch {
            Start-Sleep -Milliseconds 500
        }
    }
    throw "$Name did not become healthy at $Url within 30 seconds."
}

function Start-Backend {
    param(
        [Parameter(Mandatory = $true)] [object]$Tools,
        [Parameter(Mandatory = $true)] [object]$Config
    )

    $plainApiKey = $null
    $environmentNames = @(
        'PATCHPROOF_ANTHROPIC_API_KEY',
        'PATCHPROOF_ANTHROPIC_BASE_URL',
        'PATCHPROOF_ANTHROPIC_MODEL',
        'PATCHPROOF_LLM_TRANSPORT',
        'PATCHPROOF_CORS_ORIGINS'
    )
    $previousEnvironment = @{}
    foreach ($name in $environmentNames) {
        $previousEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
    }
    try {
        try {
            $plainApiKey = Unprotect-ApiKeyForCurrentUser -Value ([string]$Config.api_key_dpapi)
        }
        catch {
            throw 'Could not decrypt the API key. Re-run configure as the same Windows user.'
        }
        [Environment]::SetEnvironmentVariable('PATCHPROOF_ANTHROPIC_API_KEY', $plainApiKey, 'Process')
        [Environment]::SetEnvironmentVariable('PATCHPROOF_ANTHROPIC_BASE_URL', [string]$Config.base_url, 'Process')
        [Environment]::SetEnvironmentVariable('PATCHPROOF_ANTHROPIC_MODEL', [string]$Config.model, 'Process')
        [Environment]::SetEnvironmentVariable('PATCHPROOF_LLM_TRANSPORT', [string]$Config.transport, 'Process')
        [Environment]::SetEnvironmentVariable('PATCHPROOF_CORS_ORIGINS', 'http://127.0.0.1:5175,http://localhost:5175', 'Process')
        return Start-Process -FilePath $Tools.Python `
            -ArgumentList @('-m', 'uvicorn', 'patchproof.api:app', '--app-dir', 'src', '--host', '127.0.0.1', '--port', '8010') `
            -WorkingDirectory $ProjectRoot -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput $BackendOutLog -RedirectStandardError $BackendErrLog
    }
    finally {
        foreach ($name in $environmentNames) {
            [Environment]::SetEnvironmentVariable($name, $previousEnvironment[$name], 'Process')
        }
        $plainApiKey = $null
    }
}

function Start-LocalDemo {
    if ($env:OS -ne 'Windows_NT') {
        throw 'The local demo launcher requires Windows because its API key storage uses Windows DPAPI.'
    }
    $config = Read-Config
    if (-not $config) {
        Write-Host 'First run: configure the local provider.'
        Save-Configuration
        $config = Read-Config
    }

    $current = Get-CurrentStatus
    if ($current.Name -eq 'running') {
        Write-Host "PatchProof local demo is already running at $FrontendUrl."
        if (-not $NoBrowser) {
            Start-Process $FrontendUrl
        }
        return
    }
    if ($current.Name -eq 'degraded') {
        Write-Host 'A partial local demo is running; stopping only verified recorded processes before restart.'
        Stop-LocalDemo
    }
    elseif ($current.State) {
        Remove-Item -LiteralPath $StatePath -Force
    }

    if (-not (Test-PortAvailable -Port 8010)) {
        throw 'Port 8010 is already in use by an untracked process. Stop it or change the conflicting application.'
    }
    if (-not (Test-PortAvailable -Port 5175)) {
        throw 'Port 5175 is already in use by an untracked process. Stop it or change the conflicting application.'
    }

    $tools = Prepare-Dependencies
    if (-not (Test-Path -LiteralPath $RuntimeDirectory)) {
        New-Item -ItemType Directory -Path $RuntimeDirectory -Force | Out-Null
    }
    foreach ($log in @($BackendOutLog, $BackendErrLog, $FrontendOutLog, $FrontendErrLog)) {
        [System.IO.File]::WriteAllText($log, '')
    }

    $backendProcess = $null
    $frontendProcess = $null
    $backendIdentity = $null
    $frontendIdentity = $null
    try {
        Write-Host 'Starting backend on 127.0.0.1:8010...'
        $backendProcess = Start-Backend -Tools $tools -Config $config
        $backendIdentity = New-ProcessIdentity -Process $backendProcess -ExecutablePath $tools.Python
        Wait-ForEndpoint -Name 'Backend' -Url $BackendUrl -Identity $backendIdentity

        Write-Host 'Starting frontend on 127.0.0.1:5175...'
        $frontendProcess = Start-Process -FilePath $tools.Node `
            -ArgumentList @($tools.Vite, '--host', '127.0.0.1', '--port', '5175', '--strictPort') `
            -WorkingDirectory (Join-Path $ProjectRoot 'frontend') -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput $FrontendOutLog -RedirectStandardError $FrontendErrLog
        $frontendIdentity = New-ProcessIdentity -Process $frontendProcess -ExecutablePath $tools.Node

        $state = [ordered]@{
            schema_version = 1
            project_root = $ProjectRoot
            started_at_utc = [DateTime]::UtcNow.ToString('o')
            backend = $backendIdentity
            frontend = $frontendIdentity
        }
        Write-JsonFile -Path $StatePath -Value $state
        Wait-ForEndpoint -Name 'Frontend' -Url $FrontendUrl -Identity $frontendIdentity
    }
    catch {
        if ($frontendIdentity) { Stop-VerifiedProcessTree -Role 'Frontend' -Identity $frontendIdentity }
        if ($backendIdentity) { Stop-VerifiedProcessTree -Role 'Backend' -Identity $backendIdentity }
        if (Test-Path -LiteralPath $StatePath) { Remove-Item -LiteralPath $StatePath -Force }
        throw
    }

    Write-Host "PatchProof local demo is ready: $FrontendUrl"
    Write-Host "Logs: $RuntimeDirectory"
    if (-not $NoBrowser) {
        try {
            Start-Process $FrontendUrl
        }
        catch {
            Write-Warning "Could not open the browser automatically. Open $FrontendUrl manually."
        }
    }
}

function Show-Status {
    $current = Get-CurrentStatus
    switch ($current.Name) {
        'running' {
            Write-Host "PatchProof local demo: running ($FrontendUrl)"
            Write-Host "Backend PID: $($current.State.backend.pid); Frontend PID: $($current.State.frontend.pid)"
        }
        'degraded' {
            Write-Host "PatchProof local demo: degraded (backend=$($current.Backend), frontend=$($current.Frontend))"
            Write-Host "Run '.\demo.cmd stop' before restarting."
        }
        default {
            Write-Host 'PatchProof local demo: stopped.'
            if ($current.State) {
                Write-Host 'The recorded PIDs are stale and will not be terminated.'
            }
        }
    }
}

function Show-Logs {
    if (-not (Test-Path -LiteralPath $RuntimeDirectory)) {
        Write-Host 'No local demo logs exist yet.'
        return
    }
    $found = $false
    foreach ($log in @($BackendOutLog, $BackendErrLog, $FrontendOutLog, $FrontendErrLog)) {
        if (Test-Path -LiteralPath $log -PathType Leaf) {
            $found = $true
            Write-Host "`n--- $(Split-Path -Leaf $log) (last 80 lines) ---"
            Get-Content -LiteralPath $log -Tail 80
        }
    }
    if (-not $found) {
        Write-Host 'No local demo logs exist yet.'
    }
}

try {
    switch ($Action) {
        'configure' { Save-Configuration }
        'status' { Show-Status }
        'logs' { Show-Logs }
        'stop' { Stop-LocalDemo }
        default { Start-LocalDemo }
    }
}
catch {
    Write-Error $_.Exception.Message
    exit 1
}
