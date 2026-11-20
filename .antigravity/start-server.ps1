$ErrorActionPreference = 'Stop'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $ScriptDir
$Python = Join-Path $RepoRoot '.venv\Scripts\python.exe'
$ConfigPath = Join-Path $ScriptDir 'config.generated.yaml'
$LogDir = Join-Path $ScriptDir 'logs'
$PidPath = Join-Path $ScriptDir 'server.pid'
$EnvFile = Join-Path $ScriptDir 'antigravity.local.env'

if (-not (Test-Path -LiteralPath $EnvFile)) {
    throw "Create $EnvFile using antigravity.local.env.example first."
}
foreach ($entry in Get-Content -LiteralPath $EnvFile) {
    if ($entry.Trim() -and -not $entry.Trim().StartsWith('#') -and $entry -match '^([^=]+)=(.*)$') {
        $EnvName = $Matches[1].Trim()
        if (-not [Environment]::GetEnvironmentVariable($EnvName)) {
            [Environment]::SetEnvironmentVariable($EnvName, $Matches[2].Trim().Trim('"').Trim("'"), 'Process')
        }
    }
}
if (-not $env:ANTIGRAVITY_PROXY_KEY -or $env:ANTIGRAVITY_PROXY_KEY -eq 'sk-replace-with-your-local-proxy-key') {
    throw 'Set ANTIGRAVITY_PROXY_KEY to a private local proxy key.'
}
$VertexEnv = Join-Path $RepoRoot '.vertex\vertex.local.env'
foreach ($entry in Get-Content -LiteralPath $VertexEnv) {
    if ($entry -match '^\s*VERTEX_PROXY_KEY\s*=(.*)$') {
        $env:VERTEX_PROXY_KEY = $Matches[1].Trim().Trim('"').Trim("'")
    }
}
if (-not $env:VERTEX_PROXY_KEY) { throw 'VERTEX_PROXY_KEY missing from .vertex\vertex.local.env' }
$ListenHost = if ($env:ANTIGRAVITY_PROXY_HOST) { $env:ANTIGRAVITY_PROXY_HOST } else { '127.0.0.1' }
$ListenPort = if ($env:ANTIGRAVITY_PROXY_PORT) { [int]$env:ANTIGRAVITY_PROXY_PORT } else { 14606 }

function Test-OwnServer($OwnerId) {
    if (-not (Test-Path -LiteralPath $PidPath)) { return $false }
    $RecordedId = [int](Get-Content -LiteralPath $PidPath -Raw).Trim()
    $OwnerProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$OwnerId"
    return ($RecordedId -eq $OwnerId -and $OwnerProcess -and
        $OwnerProcess.CommandLine.Contains($ConfigPath) -and
        $OwnerProcess.CommandLine.Contains('litellm.proxy.proxy_cli'))
}

$Mutex = New-Object System.Threading.Mutex($false, "Local\LiteLLM-Antigravity-$ListenPort")
$Acquired = $false
try {
    $Listeners = @(Get-NetTCPConnection -State Listen -LocalPort $ListenPort -ErrorAction SilentlyContinue)
    foreach ($Listener in $Listeners) {
        if (-not (Test-OwnServer $Listener.OwningProcess)) {
            throw "Port $ListenPort is occupied by another process (PID $($Listener.OwningProcess)); no process was stopped."
        }
    }
    if ($Listeners.Count -gt 0) { Write-Output "Antigravity already running on $ListenPort"; exit 0 }
    try { $Acquired = $Mutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $Acquired = $true }
    if (-not $Acquired) { Write-Output "Antigravity launcher already active on $ListenPort"; exit 0 }
    if (-not (Test-Path -LiteralPath $Python)) { throw "Missing project Python: $Python" }
    $AgentDir = Join-Path $env:USERPROFILE '.gemini\config\agents'
    [void](New-Item -ItemType Directory -Path $AgentDir -Force)
    $AgentBody = Get-Content -LiteralPath (Join-Path $ScriptDir 'agent.md') -Raw
    $AgentPath = Join-Path $AgentDir 'litellm-antigravity.md'
    if (-not (Test-Path -LiteralPath $AgentPath) -or (Get-Content -LiteralPath $AgentPath -Raw) -cne $AgentBody) {
        [System.IO.File]::WriteAllText($AgentPath, $AgentBody, (New-Object System.Text.UTF8Encoding($false)))
    }
    $env:PYTHONIOENCODING = 'utf-8'
    $env:PYTHONPATH = "$ScriptDir;$RepoRoot"
    $env:LITELLM_LOCAL_MODEL_COST_MAP = 'True'
    [void](New-Item -ItemType Directory -Path $LogDir -Force)
    & $Python (Join-Path $ScriptDir 'generate_config.py')
    if ($LASTEXITCODE -ne 0) { throw 'Antigravity model config generation failed.' }
    $Arguments = @('-m', 'litellm.proxy.proxy_cli', '--config', "`"$ConfigPath`"", '--host', $ListenHost, '--port', $ListenPort)
    $Server = Start-Process -FilePath $Python -ArgumentList $Arguments -WorkingDirectory $RepoRoot -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $LogDir 'server.log') -RedirectStandardError (Join-Path $LogDir 'server.err.log')
    $Deadline = (Get-Date).AddSeconds(60)
    $Ready = $false
    while ((Get-Date) -lt $Deadline -and -not $Server.HasExited) {
        $StartedListeners = @(Get-NetTCPConnection -State Listen -LocalPort $ListenPort -ErrorAction SilentlyContinue)
        if ($StartedListeners.Count -gt 0) {
            $OwnerId = $StartedListeners[0].OwningProcess
            $OwnerProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$OwnerId"
            if (-not $OwnerProcess.CommandLine.Contains($ConfigPath) -or
                ($OwnerId -ne $Server.Id -and $OwnerProcess.ParentProcessId -ne $Server.Id)) {
                Stop-Process -Id $Server.Id -ErrorAction SilentlyContinue
                throw "Port $ListenPort was taken by another process (PID $OwnerId) during startup."
            }
            Set-Content -LiteralPath $PidPath -Value $OwnerId
            $Ready = $true
            Write-Output "Antigravity running on $ListenHost`:$ListenPort (PID $OwnerId)"
            break
        }
        Start-Sleep -Milliseconds 300
        $Server.Refresh()
    }
    if (-not $Ready) {
        if (-not $Server.HasExited) { Stop-Process -Id $Server.Id }
        throw "Antigravity failed to listen on port $ListenPort; see $LogDir"
    }
    $Server.WaitForExit()
    $Server.Refresh()
    if ($Server.ExitCode -ne 0) { throw "Antigravity server exited with code $($Server.ExitCode); see $LogDir" }
} finally {
    if ($Acquired) { $Mutex.ReleaseMutex() }
    $Mutex.Dispose()
}
