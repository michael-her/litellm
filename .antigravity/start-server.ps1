param([switch]$RunSupervisor)
$ErrorActionPreference = 'Stop'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $ScriptDir
$Python = Join-Path $RepoRoot '.venv\Scripts\python.exe'
$ConfigPath = Join-Path $ScriptDir 'config.generated.yaml'
$LogDir = Join-Path $ScriptDir 'logs'
$PidPath = Join-Path $ScriptDir 'server.pid'
$EnvFile = Join-Path $ScriptDir 'antigravity.local.env'
$SupervisorPath = Join-Path $ScriptDir 'supervisor.pid'
$StopPath = Join-Path $ScriptDir 'stop.requested'
$SupervisorLog = Join-Path $LogDir 'supervisor.log'

function Write-SupervisorLog([string]$Message) {
    Add-Content -LiteralPath $SupervisorLog -Value ("{0:o} {1}" -f (Get-Date), $Message)
}

function Stop-ServerTree([int]$ServerId) {
    if (-not (Get-Process -Id $ServerId -ErrorAction SilentlyContinue)) { return }
    try {
        & (Join-Path $env:SystemRoot 'System32\taskkill.exe') /PID $ServerId /T /F 2>&1 | Out-Null
    } catch {
        Write-SupervisorLog "Server cleanup PID=$ServerId failed: $($_.Exception.Message)"
    }
}

function Test-Supervisor {
    if (-not (Test-Path -LiteralPath $SupervisorPath)) { return $false }
    $RecordedId = [int](Get-Content -LiteralPath $SupervisorPath -Raw).Trim()
    $Owner = Get-CimInstance Win32_Process -Filter "ProcessId=$RecordedId"
    return ($Owner -and $Owner.CommandLine.Contains($ScriptDir) -and $Owner.CommandLine.Contains('-RunSupervisor'))
}

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

if ($RunSupervisor -and (Test-Path -LiteralPath (Join-Path $ScriptDir 'launch.generated.json'))) {
    $LaunchSettings = Get-Content -LiteralPath (Join-Path $ScriptDir 'launch.generated.json') -Raw | ConvertFrom-Json
    $ListenHost = $LaunchSettings.host
    $ListenPort = [int]$LaunchSettings.port
    Remove-Item -LiteralPath (Join-Path $ScriptDir 'launch.generated.json') -Force
}
$Listeners = @(Get-NetTCPConnection -State Listen -LocalPort $ListenPort -ErrorAction SilentlyContinue)
foreach ($Listener in $Listeners) {
    if (-not (Test-OwnServer $Listener.OwningProcess)) {
        throw "Port $ListenPort is occupied by another process (PID $($Listener.OwningProcess)); no process was stopped."
    }
}
if (-not $RunSupervisor) {
    if (Test-Supervisor) { Write-Output "Antigravity supervisor already running on $ListenPort"; exit 0 }
    if ($Listeners.Count -gt 0) { throw 'Legacy Antigravity server has no supervisor; use stop-server.ps1 first.' }
    & (Join-Path $ScriptDir 'install-startup.ps1') | Out-Null
    $ShortcutPath = Join-Path ([Environment]::GetFolderPath('Startup')) 'litellm.exe antigravity.lnk'
    [System.IO.File]::WriteAllText((Join-Path $ScriptDir 'launch.generated.json'), (@{host=$ListenHost; port=$ListenPort} | ConvertTo-Json))
    Start-Process -FilePath (Join-Path $env:SystemRoot 'explorer.exe') -ArgumentList ('"' + $ShortcutPath + '"') -WindowStyle Hidden
    $Deadline = (Get-Date).AddSeconds(90)
    while ((Get-Date) -lt $Deadline) {
        if ((Test-Supervisor) -and (Test-Path -LiteralPath $PidPath)) {
            $ReadyId = [int](Get-Content -LiteralPath $PidPath -Raw).Trim()
            if (Test-OwnServer $ReadyId) {
                Write-Output "Antigravity detached supervisor and server running on $ListenPort (PID $ReadyId)"
                exit 0
            }
        }
        Start-Sleep -Milliseconds 500
    }
    throw "Detached Antigravity launch failed; see $SupervisorLog and server.err.log"
}
$Mutex = New-Object System.Threading.Mutex($false, "Local\LiteLLM-Antigravity-$ListenPort")
$Acquired = $false
$Server = $null
try {
    $Listeners = @(Get-NetTCPConnection -State Listen -LocalPort $ListenPort -ErrorAction SilentlyContinue)
    foreach ($Listener in $Listeners) {
        if (-not (Test-OwnServer $Listener.OwningProcess)) {
            throw "Port $ListenPort is occupied by another process (PID $($Listener.OwningProcess)); no process was stopped."
        }
    }
    try { $Acquired = $Mutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $Acquired = $true }
    if (-not $Acquired) { Write-Output "Antigravity launcher already active on $ListenPort"; exit 0 }
    [void](New-Item -ItemType Directory -Path $LogDir -Force)
    Remove-Item -LiteralPath $StopPath -Force -ErrorAction SilentlyContinue
    Set-Content -LiteralPath $SupervisorPath -Value $PID
    Write-SupervisorLog "Supervisor started PID=$PID"
    if ($env:ANTIGRAVITY_CLI -or $env:ANTIGRAVITY_CLI_ARGS) {
        throw 'Production supervisor refuses ANTIGRAVITY_CLI/ANTIGRAVITY_CLI_ARGS test overrides.'
    }
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
    $Backoff = 5
    while (-not (Test-Path -LiteralPath $StopPath)) {
        $StartedAt = Get-Date
        Write-SupervisorLog "Starting server on $ListenHost`:$ListenPort"
        $Arguments = @('-m', 'litellm.proxy.proxy_cli', '--config', "`"$ConfigPath`"", '--host', $ListenHost, '--port', $ListenPort)
        $Server = Start-Process -FilePath $Python -ArgumentList $Arguments -WorkingDirectory $RepoRoot -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput (Join-Path $LogDir 'server.log') -RedirectStandardError (Join-Path $LogDir 'server.err.log')
        Set-Content -LiteralPath (Join-Path $ScriptDir 'server-launcher.pid') -Value $Server.Id
        $Deadline = (Get-Date).AddSeconds(60)
        $Ready = $false
        while ((Get-Date) -lt $Deadline -and -not $Server.HasExited -and -not (Test-Path -LiteralPath $StopPath)) {
            $StartedListeners = @(Get-NetTCPConnection -State Listen -LocalPort $ListenPort -ErrorAction SilentlyContinue)
            if ($StartedListeners.Count -gt 0) {
                $OwnerId = $StartedListeners[0].OwningProcess
                $OwnerProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$OwnerId"
                if (-not $OwnerProcess.CommandLine.Contains($ConfigPath) -or
                    ($OwnerId -ne $Server.Id -and $OwnerProcess.ParentProcessId -ne $Server.Id)) {
                    Stop-ServerTree $Server.Id
                    throw "Port $ListenPort was taken by another process (PID $OwnerId) during startup."
                }
                Set-Content -LiteralPath $PidPath -Value $OwnerId
                $Ready = $true
                Write-Output "Antigravity running on $ListenHost`:$ListenPort (PID $OwnerId)"
                Write-SupervisorLog "Ready server PID=$OwnerId launcher=$($Server.Id) supervisor=$PID"
                break
            }
            Start-Sleep -Milliseconds 300
            $Server.Refresh()
        }
        while ($Ready -and -not $Server.HasExited -and -not (Test-Path -LiteralPath $StopPath)) {
            Start-Sleep -Milliseconds 500
            $Server.Refresh()
        }
        if (Test-Path -LiteralPath $StopPath) { break }
        if (-not $Server.HasExited) {
            Stop-ServerTree $Server.Id
        }
        $Server.WaitForExit()
        Remove-Item -LiteralPath $PidPath -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath (Join-Path $ScriptDir 'server-launcher.pid') -Force -ErrorAction SilentlyContinue
        if (((Get-Date) - $StartedAt).TotalSeconds -ge 60) { $Backoff = 5 }
        Write-SupervisorLog "Unexpected server exit; restarting in $Backoff seconds"
        $RetryAt = (Get-Date).AddSeconds($Backoff)
        while ((Get-Date) -lt $RetryAt -and -not (Test-Path -LiteralPath $StopPath)) { Start-Sleep -Milliseconds 250 }
        $Backoff = [Math]::Min(60, $Backoff * 2)
    }
} catch {
    Write-SupervisorLog "Supervisor failed: $($_.Exception.Message)"
    throw
} finally {
    if ($Acquired) {
        if ($Server -and -not $Server.HasExited) {
            Stop-ServerTree $Server.Id
        }
        Remove-Item -LiteralPath $PidPath -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath (Join-Path $ScriptDir 'server-launcher.pid') -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $SupervisorPath -Force -ErrorAction SilentlyContinue
        Write-SupervisorLog "Supervisor stopped PID=$PID"
        $Mutex.ReleaseMutex()
    }
    $Mutex.Dispose()
}
