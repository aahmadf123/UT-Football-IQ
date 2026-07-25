# Syncs the JWT signing secret between the Fly backend and the Cloudflare
# Worker so the worker can verify backend-issued JWTs (uploads/downloads).
#
#   .\scripts\sync-worker-secret.ps1           # copy Fly's current SECRET_KEY -> worker JWT_SECRET
#   .\scripts\sync-worker-secret.ps1 -Rotate   # generate a fresh secret and set it on BOTH sides
#
# The secret value is never displayed. -Rotate invalidates existing login
# sessions (users just sign in again).

[CmdletBinding()]
param(
    [string]$AppName = "football-iq-backend",
    [switch]$Rotate
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Resolve-Path (Join-Path $scriptDir "..")
$workersDir = Join-Path $repoRoot "workers"

if ($Rotate) {
    Write-Host "Generating a fresh 64-char secret and rotating both sides..." -ForegroundColor Cyan
    $bytes = [byte[]]::new(32)
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    $secret = -join ($bytes | ForEach-Object { $_.ToString('x2') })

    "SECRET_KEY=$secret" | flyctl secrets import -a $AppName
    if ($LASTEXITCODE -ne 0) { throw "flyctl secrets import failed" }
    Write-Host "Fly SECRET_KEY updated (machines restarting)." -ForegroundColor Green
}
else {
    Write-Host "Reading current SECRET_KEY from the running Fly machine..." -ForegroundColor Cyan
    # flyctl writes "Connecting to ..." progress to stderr; under strict mode
    # Windows PowerShell 5.1 turns captured stderr into terminating errors, so
    # relax the preference just for this call.
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $out = & flyctl ssh console -a $AppName -C "printenv SECRET_KEY" 2>$null
    $ErrorActionPreference = $prevEap
    $secret = ($out | Where-Object { $_ -and $_.ToString().Trim() } | Select-Object -Last 1).ToString().Trim()
    if (-not $secret -or $secret.Length -lt 16) {
        throw "Could not read SECRET_KEY from Fly (got $($secret.Length) chars). Try -Rotate instead."
    }
    Write-Host "Captured SECRET_KEY ($($secret.Length) chars)." -ForegroundColor Green
}

Write-Host "Setting worker secret JWT_SECRET..." -ForegroundColor Cyan
Push-Location $workersDir
try {
    $secret | wrangler secret put JWT_SECRET
    if ($LASTEXITCODE -ne 0) { throw "wrangler secret put failed" }
}
finally {
    Pop-Location
}

Write-Host "`nDone. Verifying the worker accepts a backend-issued JWT..." -ForegroundColor Cyan
$email = "secret-sync-" + [Guid]::NewGuid().ToString("N").Substring(0, 8) + "@example.com"
$baseUrl = "https://$AppName.fly.dev"
$body = @{ email = $email; password = "pw-123456"; full_name = "Secret Sync Check" } | ConvertTo-Json
Invoke-RestMethod -Uri "$baseUrl/api/v1/auth/register" -Method Post -ContentType "application/json" -Body $body | Out-Null
$login = @{ email = $email; password = "pw-123456" } | ConvertTo-Json
$token = (Invoke-RestMethod -Uri "$baseUrl/api/v1/auth/login" -Method Post -ContentType "application/json" -Body $login).access_token

$resp = Invoke-RestMethod -Uri "https://football-iq.firas-azfar.workers.dev/api/v1/videos/upload-url" `
    -Method Post -ContentType "application/json" `
    -Headers @{ Authorization = "Bearer $token" } `
    -Body (@{ filename = "sync-check.mp4" } | ConvertTo-Json)

if ($resp.uploadUrl) {
    Write-Host "Worker accepted the JWT and returned an upload URL. All good." -ForegroundColor Green
}
else {
    throw "Worker response missing uploadUrl: $($resp | ConvertTo-Json -Compress)"
}
