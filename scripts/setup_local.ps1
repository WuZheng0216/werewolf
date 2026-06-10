param(
    [string]$EnvName = "werewolf",
    [switch]$SkipCondaCreate,
    [switch]$SkipInstall
)

$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $RepoRoot

Write-Host "AI Werewolf local setup"
Write-Host "Repo: $RepoRoot"

$conda = Get-Command conda -ErrorAction SilentlyContinue
if ($null -eq $conda) {
    Write-Host "Conda was not found. Please install Miniconda/Anaconda, then rerun this script." -ForegroundColor Yellow
    Write-Host "Fallback: use any Python 3.12+ environment and run: python -m pip install -r requirements.txt"
    exit 1
}

if (-not $SkipCondaCreate) {
    $envList = conda env list | Out-String
    if ($envList -notmatch "(^|\s)$([regex]::Escape($EnvName))(\s|$)") {
        Write-Host "Creating conda environment: $EnvName"
        conda create -n $EnvName python=3.12 -y
    } else {
        Write-Host "Conda environment already exists: $EnvName"
    }
}

if (-not (Test-Path ".env")) {
    if (Test-Path ".env.example") {
        Copy-Item ".env.example" ".env"
        Write-Host "Created .env from .env.example"
    } else {
        Write-Host ".env.example not found; please create .env manually." -ForegroundColor Yellow
    }
} else {
    Write-Host ".env already exists; not overwriting it."
}

if (-not $SkipInstall) {
    Write-Host "Installing Python dependencies into conda env: $EnvName"
    conda run -n $EnvName python -m pip install -r requirements.txt
}

Write-Host ""
Write-Host "Setup complete."
Write-Host "Next steps:"
Write-Host "1. Open .env and replace API placeholders with your own keys."
Write-Host "2. Start the app:"
Write-Host "   powershell -ExecutionPolicy Bypass -File scripts\start_local.ps1"
Write-Host "3. Open http://127.0.0.1:8000"

