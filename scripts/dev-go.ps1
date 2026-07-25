[CmdletBinding()]
param(
    [switch]$WithCloudflare,
    [switch]$SkipInstall,
    [switch]$UsePipelineStub
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$devUp = Join-Path $scriptDir "dev-up.ps1"
$devDoctor = Join-Path $scriptDir "dev-doctor.ps1"

if (-not (Test-Path $devUp)) {
    throw "Missing script: $devUp"
}
if (-not (Test-Path $devDoctor)) {
    throw "Missing script: $devDoctor"
}

Write-Host "Starting Football-IQ stack..." -ForegroundColor Cyan
$upParams = @{}
if ($WithCloudflare) { $upParams.WithCloudflare = $true }
if ($SkipInstall) { $upParams.SkipInstall = $true }
if ($UsePipelineStub) { $upParams.UsePipelineStub = $true }

& $devUp @upParams
if ($LASTEXITCODE -ne 0) {
    throw "Startup failed."
}

Write-Host "Running health verification..." -ForegroundColor Cyan
$doctorParams = @{}
if ($WithCloudflare) { $doctorParams.WithCloudflare = $true }

& $devDoctor @doctorParams
if ($LASTEXITCODE -ne 0) {
    throw "Health check failed."
}

Write-Host "Football-IQ is ready." -ForegroundColor Green
Write-Host "Backend:   http://localhost:8000/health" -ForegroundColor Yellow
Write-Host "Frontend:  http://localhost:3000" -ForegroundColor Yellow
if ($WithCloudflare) {
    Write-Host "Worker:    http://127.0.0.1:8787" -ForegroundColor Yellow
}
