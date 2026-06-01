# Build delivery ZIP (no .venv, no data artifacts, no cache).
# Run from repo root: .\pack-delivery.ps1

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$Dist = Join-Path $Root "dist"
$ZipName = "altiora_1_stage_delivery.zip"
$ZipPath = Join-Path $Dist $ZipName
$Staging = Join-Path $Dist "staging"

if (Test-Path $Staging) { Remove-Item -Recurse -Force $Staging }
New-Item -ItemType Directory -Force -Path $Staging, $Dist | Out-Null

$Include = @(
    "api",
    "scripts",
    "docs",
    "data",
    "requirements.txt",
    "Dockerfile",
    "docker-compose.yml",
    ".dockerignore",
    ".gitignore",
    "README.md"
)

foreach ($item in $Include) {
    $src = Join-Path $Root $item
    if (-not (Test-Path $src)) {
        Write-Warning "Skip missing: $item"
        continue
    }
    Copy-Item -Path $src -Destination (Join-Path $Staging $item) -Recurse -Force
}

# Strip runtime files from data/ if any slipped in
$dataRoot = Join-Path $Staging "data"
if (Test-Path $dataRoot) {
    Get-ChildItem $dataRoot -Recurse -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -ne ".gitkeep" } |
        Remove-Item -Force -ErrorAction SilentlyContinue
    Get-ChildItem $dataRoot -Recurse -Directory -ErrorAction SilentlyContinue |
        Where-Object { (Get-ChildItem $_.FullName -Force -ErrorAction SilentlyContinue).Count -eq 0 } |
        ForEach-Object {
            $keep = Join-Path $_.FullName ".gitkeep"
            if (-not (Test-Path $keep)) {
                New-Item -ItemType File -Force -Path $keep | Out-Null
            }
        }
}

if (Test-Path $ZipPath) { Remove-Item -Force $ZipPath }
Compress-Archive -Path (Join-Path $Staging "*") -DestinationPath $ZipPath -Force
Remove-Item -Recurse -Force $Staging

$sizeMb = [math]::Round((Get-Item $ZipPath).Length / 1MB, 2)
Write-Host "OK: $ZipPath ($sizeMb MB)"
