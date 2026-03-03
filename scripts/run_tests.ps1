$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot\..

if (Get-Command pytest -ErrorAction SilentlyContinue) {
  pytest -q
  exit $LASTEXITCODE
}

Write-Host "pytest not found, skipping tests"
exit 0

