$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot\..

$serviceName = $env:PHOENIX_SERVICE_NAME
if ([string]::IsNullOrWhiteSpace($serviceName)) {
  $serviceName = "PhoenixAgent"
}

$service = Get-Service -Name $serviceName -ErrorAction SilentlyContinue
if ($null -eq $service) {
  Write-Host "Service '$serviceName' not found. Skipping restart."
  exit 0
}

if ($service.Status -eq "Running") {
  Restart-Service -Name $serviceName -Force
} else {
  Start-Service -Name $serviceName
}

Write-Host "Service '$serviceName' restart done."
exit 0

