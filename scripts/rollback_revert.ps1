$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot\..

git revert --no-edit HEAD
if ($LASTEXITCODE -ne 0) {
  Write-Error "git revert failed"
  exit $LASTEXITCODE
}

$remote = $env:PHOENIX_REMOTE_NAME
if ([string]::IsNullOrWhiteSpace($remote)) {
  $remote = "origin"
}

$branch = $env:PHOENIX_MAIN_BRANCH
if ([string]::IsNullOrWhiteSpace($branch)) {
  $branch = "main"
}

git push $remote $branch
exit $LASTEXITCODE

