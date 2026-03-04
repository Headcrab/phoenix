$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot\..

if (Get-Command pytest -ErrorAction SilentlyContinue) {
  $baseTemp = ".pytest-local-check"
  $cacheDir = "$baseTemp/.cache"
  New-Item -ItemType Directory -Path $baseTemp -Force | Out-Null
  New-Item -ItemType Directory -Path $cacheDir -Force | Out-Null
  pytest -q --basetemp $baseTemp -o "cache_dir=$cacheDir"
  exit $LASTEXITCODE
}

Write-Host "pytest not found, skipping tests"
exit 0
