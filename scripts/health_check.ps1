$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot\..

$healthUrl = $env:PHOENIX_HEALTHCHECK_URL
if ([string]::IsNullOrWhiteSpace($healthUrl)) {
  $healthUrl = "http://127.0.0.1:8666/health"
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
  Write-Error "Health-check failed: $($_.Exception.Message)"
  exit 1
}
