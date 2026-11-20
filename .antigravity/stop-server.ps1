$ErrorActionPreference = 'Stop'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$StopPath = Join-Path $ScriptDir 'stop.requested'
Set-Content -LiteralPath $StopPath -Value (Get-Date -Format o)
$SupervisorPath = Join-Path $ScriptDir 'supervisor.pid'
if (Test-Path -LiteralPath $SupervisorPath) {
    $SupervisorId = [int](Get-Content -LiteralPath $SupervisorPath -Raw).Trim()
    $Supervisor = Get-CimInstance Win32_Process -Filter "ProcessId=$SupervisorId"
    if ($Supervisor -and $Supervisor.CommandLine.Contains($ScriptDir) -and $Supervisor.CommandLine.Contains('-RunSupervisor')) {
        $Deadline = (Get-Date).AddSeconds(15)
        while ((Get-Date) -lt $Deadline -and (Get-Process -Id $SupervisorId -ErrorAction SilentlyContinue)) {
            Start-Sleep -Milliseconds 250
        }
        if (Get-Process -Id $SupervisorId -ErrorAction SilentlyContinue) {
            & (Join-Path $env:SystemRoot 'System32\taskkill.exe') /PID $SupervisorId /T /F 2>&1 | Out-Null
        }
    }
}
foreach ($Name in @('server-launcher.pid', 'server.pid')) {
    $PidPath = Join-Path $ScriptDir $Name
    if (-not (Test-Path -LiteralPath $PidPath)) { continue }
    $OwnerId = [int](Get-Content -LiteralPath $PidPath -Raw).Trim()
    $Owner = Get-CimInstance Win32_Process -Filter "ProcessId=$OwnerId"
    if ($Owner) {
        if (-not $Owner.CommandLine.Contains((Join-Path $ScriptDir 'config.generated.yaml')) -or
            -not $Owner.CommandLine.Contains('litellm.proxy.proxy_cli')) {
            throw "Refusing to stop unrelated PID $OwnerId from $PidPath"
        }
        & (Join-Path $env:SystemRoot 'System32\taskkill.exe') /PID $OwnerId /T /F 2>&1 | Out-Null
    }
    Remove-Item -LiteralPath $PidPath -Force -ErrorAction SilentlyContinue
}
Remove-Item -LiteralPath $SupervisorPath -Force -ErrorAction SilentlyContinue
Write-Output 'Antigravity supervisor and server stopped; stop.requested prevents watchdog restart.'
