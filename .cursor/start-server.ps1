# Start LiteLLM proxy with .cursor/config.yaml (Cursor SDK + Vertex models).
$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $ScriptDir
$ConfigPath = Join-Path $ScriptDir "config.yaml"
$EnvFile = Join-Path $ScriptDir "cursor.local.env"

$env:PYTHONIOENCODING = "utf-8"

if (Test-Path $EnvFile) {
    Get-Content $EnvFile | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith("#") -and $line -match "^([^=]+)=(.*)$") {
            $key = $Matches[1].Trim()
            $value = $Matches[2].Trim().Trim('"').Trim("'")
            if ($key -and -not [Environment]::GetEnvironmentVariable($key)) {
                [Environment]::SetEnvironmentVariable($key, $value, "Process")
            }
        }
    }
} else {
    Write-Warning "Missing $EnvFile — copy from cursor.local.env.example and set CURSOR_API_KEY."
}

Set-Location $RepoRoot

$ListenPort = if ($env:LITELLM_PORT) { $env:LITELLM_PORT } else { "4001" }
$ListenHost = if ($env:LITELLM_HOST) { $env:LITELLM_HOST } else { "127.0.0.1" }

Write-Host "Starting LiteLLM proxy at http://${ListenHost}:${ListenPort}" -ForegroundColor Cyan
Write-Host "Config: $ConfigPath" -ForegroundColor DarkGray

uv run litellm --config $ConfigPath --port $ListenPort --host $ListenHost
