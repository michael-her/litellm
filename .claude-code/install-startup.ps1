$ErrorActionPreference = 'Stop'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$StartupDir = [Environment]::GetFolderPath('Startup')
$ShortcutPath = Join-Path $StartupDir 'litellm.exe claude.lnk'
$Shell = New-Object -ComObject WScript.Shell
$Shortcut = $Shell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$Shortcut.Arguments = "-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$(Join-Path $ScriptDir 'start-server.ps1')`""
$Shortcut.WorkingDirectory = Split-Path -Parent $ScriptDir
$Shortcut.WindowStyle = 7
$Shortcut.Description = 'LiteLLM Claude Code CLI proxy on localhost:14605'
$Shortcut.Save()
Write-Output $ShortcutPath
