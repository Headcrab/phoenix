$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot\..

if (Get-Command ruff -ErrorAction SilentlyContinue) {
  ruff check app tests
  exit $LASTEXITCODE
}

Write-Host "ruff not found, running compileall fallback"
python -m compileall app tests
exit $LASTEXITCODE

