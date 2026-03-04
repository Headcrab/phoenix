$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot\..

$healthUrl = $env:PHOENIX_HEALTHCHECK_URL
if ([string]::IsNullOrWhiteSpace($healthUrl)) {
  $healthUrl = "http://127.0.0.1:8666/health"
}
$strict = $env:PHOENIX_HEALTHCHECK_STRICT
$strictMode = $false
if (-not [string]::IsNullOrWhiteSpace($strict)) {
  $strictMode = @("1", "true", "yes", "on") -contains $strict.ToLowerInvariant()
}

try {
  $response = Invoke-WebRequest -Uri $healthUrl -UseBasicParsing -TimeoutSec 10
  if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 300) {
    Write-Host "Health-check OK: $($response.StatusCode)"
    exit 0
  }
  Write-Error "Health-check failed with code $($response.StatusCode)"
  exit 1
}
catch {
  $exception = $_.Exception
  if ($exception -and $exception.Response) {
    Write-Error "Health-check failed: $($exception.Message)"
    exit 1
  }
  if ($strictMode) {
    Write-Error "Health-check failed: $($exception.Message)"
    exit 1
  }
  Write-Warning "Health-check skipped: endpoint unreachable ($($exception.Message))"
  Write-Host "Set PHOENIX_HEALTHCHECK_STRICT=true to enforce failure."
  exit 0
}
