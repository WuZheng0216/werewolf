param(
    [string]$Output = "dist\werewolf-local-demo.zip"
)

$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $RepoRoot

$outputPath = [System.IO.Path]::GetFullPath((Join-Path $RepoRoot $Output))
$distRoot = Join-Path $RepoRoot "dist"
$stageRoot = Join-Path $distRoot "_werewolf_local_demo_package"

New-Item -ItemType Directory -Force -Path $distRoot | Out-Null

$resolvedDist = [System.IO.Path]::GetFullPath($distRoot)
$resolvedStage = [System.IO.Path]::GetFullPath($stageRoot)
if (-not $resolvedStage.StartsWith($resolvedDist, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to clean staging path outside dist: $resolvedStage"
}

if (Test-Path $stageRoot) {
    Remove-Item -LiteralPath $stageRoot -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $stageRoot | Out-Null

$items = @(
    "werewolf_ai",
    "static",
    "scripts",
    "data\memory\latest_promoted.json",
    "data\memory\role_memory_evolved_skill_r12_v2.json",
    "data\memory\memory_bank.json",
    "requirements.txt",
    "server.py",
    ".env.example",
    "LOCAL_QUICKSTART.md",
    "README.md"
)

foreach ($item in $items) {
    if (-not (Test-Path $item)) {
        Write-Host "Skip missing item: $item" -ForegroundColor Yellow
        continue
    }
    $source = Resolve-Path $item
    $target = Join-Path $stageRoot $item
    $targetParent = Split-Path $target -Parent
    New-Item -ItemType Directory -Force -Path $targetParent | Out-Null
    Copy-Item -LiteralPath $source -Destination $target -Recurse -Force
}

Get-ChildItem -Path $stageRoot -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force
Get-ChildItem -Path $stageRoot -Recurse -File -Include "*.pyc","*.pyo" | Remove-Item -Force

if (Test-Path $outputPath) {
    Remove-Item -LiteralPath $outputPath -Force
}

Compress-Archive -Path (Join-Path $stageRoot "*") -DestinationPath $outputPath -Force
Remove-Item -LiteralPath $stageRoot -Recurse -Force

Write-Host "Created local demo package:"
Write-Host $outputPath
Write-Host ""
Write-Host "The package excludes .env and logs. The receiver should edit .env after setup."

