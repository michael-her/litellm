$ErrorActionPreference = 'Stop'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $ScriptDir
$EnvFile = Join-Path $ScriptDir 'claude.local.env'
$Python = Join-Path $RepoRoot '.venv\Scripts\python.exe'
$ConfigPath = Join-Path $ScriptDir 'config.yaml'
$LogDir = Join-Path $ScriptDir 'logs'

if (-not (Test-Path -LiteralPath $EnvFile)) {
    throw "Create $EnvFile using claude.local.env.example first."
}
foreach ($entry in Get-Content -LiteralPath $EnvFile) {
    if ($entry.Trim() -and -not $entry.Trim().StartsWith('#') -and $entry -match '^([^=]+)=(.*)$') {
        $EnvName = $Matches[1].Trim()
        $EnvValue = $Matches[2].Trim().Trim('"').Trim("'")
        [Environment]::SetEnvironmentVariable($EnvName, $EnvValue, 'Process')
    }
}
if (-not $env:CLAUDE_PROXY_KEY -or $env:CLAUDE_PROXY_KEY -eq 'sk-replace-with-your-local-proxy-key') {
    throw 'Set CLAUDE_PROXY_KEY to a private local proxy key.'
}
if (-not $env:CLAUDE_CODE_OAUTH_TOKEN) {
    throw 'Set CLAUDE_CODE_OAUTH_TOKEN to the token printed by claude setup-token.'
}
if (-not (Test-Path -LiteralPath $Python)) {
    throw "Missing project Python environment: $Python"
}
$ListenHost = if ($env:CLAUDE_PROXY_HOST) { $env:CLAUDE_PROXY_HOST } else { '127.0.0.1' }
$ListenPort = if ($env:CLAUDE_PROXY_PORT) { $env:CLAUDE_PROXY_PORT } else { '14605' }
$Mutex = New-Object System.Threading.Mutex($false, "Local\LiteLLM-Claude-$ListenPort")
$Acquired = $false
try {
    try { $Acquired = $Mutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $Acquired = $true }
    if (-not $Acquired) { exit 0 }
    $env:PYTHONIOENCODING = 'utf-8'
    $env:PYTHONPATH = "$ScriptDir;$RepoRoot"
    $env:LITELLM_LOCAL_MODEL_COST_MAP = 'True'
    [void](New-Item -ItemType Directory -Path $LogDir -Force)
    Set-Location -LiteralPath $RepoRoot
    $Arguments = @('-m', 'litellm.proxy.proxy_cli', '--config', "`"$ConfigPath`"", '--host', $ListenHost, '--port', $ListenPort)
    $Server = Start-Process -FilePath $Python -ArgumentList $Arguments -WorkingDirectory $RepoRoot -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $LogDir 'server.log') -RedirectStandardError (Join-Path $LogDir 'server.err.log')
    Set-Content -LiteralPath (Join-Path $ScriptDir 'server.pid') -Value $Server.Id
    $Server.WaitForExit()
    exit $Server.ExitCode
} finally {
    if ($Acquired) { $Mutex.ReleaseMutex() }
    $Mutex.Dispose()
}
