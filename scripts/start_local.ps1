param(
    [string]$EnvName = "werewolf",
    [string]$HostName = "127.0.0.1",
    [int]$Port = 8000,
    [switch]$NoConda
)

$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $RepoRoot

if (-not (Test-Path ".env")) {
    Write-Host ".env not found. Creating it from .env.example..." -ForegroundColor Yellow
    if (Test-Path ".env.example") {
        Copy-Item ".env.example" ".env"
        Write-Host "Please edit .env and fill at least one model API key before running again." -ForegroundColor Yellow
        exit 1
    }
    Write-Host ".env.example not found. Please create .env manually." -ForegroundColor Red
    exit 1
}

$url = "http://$HostName`:$Port"
Write-Host "Starting AI Werewolf server at $url"
Write-Host "Press Ctrl+C to stop."

if ($NoConda) {
    python server.py --host $HostName --port $Port
} else {
    conda run -n $EnvName python server.py --host $HostName --port $Port
}

