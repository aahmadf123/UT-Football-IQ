[CmdletBinding()]
param(
    [string]$AppName = "football-iq-backend",
    [string]$BackendDir = "backend",
    [switch]$SkipDeploy,
    [switch]$SkipMigrate
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Write-Step {
    param([string]$Message)
    Write-Host "`n==> $Message" -ForegroundColor Cyan
}

function Resolve-FlyctlPath {
    $cmd = Get-Command flyctl -ErrorAction SilentlyContinue
    if ($cmd) {
        return $cmd.Source
    }

    $wingetRoot = "$env:LOCALAPPDATA/Microsoft/WinGet/Packages/Fly-io.flyctl_Microsoft.Winget.Source_8wekyb3d8bbwe"
    $candidate = Join-Path $wingetRoot "flyctl.exe"
    if (Test-Path $candidate) {
        return $candidate
    }

    throw "flyctl not found. Install it with: winget install --id Fly-io.flyctl -e"
}

function Invoke-Checked {
    param(
        [string]$FilePath,
        [string[]]$Arguments
    )
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed: $FilePath $($Arguments -join ' ')"
    }
}

function Test-CorsPreflight {
    param(
        [string]$BaseUrl,
        [string]$Origin
    )

    try {
        $resp = Invoke-WebRequest -Uri "$BaseUrl/api/v1/auth/register" -Method Options -Headers @{
            Origin = $Origin
            "Access-Control-Request-Method" = "POST"
            "Access-Control-Request-Headers" = "content-type"
        } -UseBasicParsing -TimeoutSec 20

        $allowOrigin = [string]$resp.Headers["Access-Control-Allow-Origin"]
        return [pscustomobject]@{
            Origin = $Origin
            Status = [int]$resp.StatusCode
            AllowOrigin = $allowOrigin
            Passed = ($resp.StatusCode -eq 200 -and $allowOrigin -eq $Origin)
        }
    }
    catch {
        $status = 0
        $allowOrigin = ""
        if ($_.Exception.Response) {
            $status = [int]$_.Exception.Response.StatusCode
            $allowOrigin = [string]$_.Exception.Response.Headers["Access-Control-Allow-Origin"]
        }
        return [pscustomobject]@{
            Origin = $Origin
            Status = $status
            AllowOrigin = $allowOrigin
            Passed = $false
        }
    }
}

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Resolve-Path (Join-Path $scriptDir "..")
Set-Location $repoRoot

$fly = Resolve-FlyctlPath
$baseUrl = "https://$AppName.fly.dev"

Write-Step "Using flyctl: $fly"

Write-Step "Checking Fly authentication"
$whoami = & $fly auth whoami 2>&1
if ($LASTEXITCODE -ne 0) {
    throw "Fly auth check failed. Run '$fly auth login' once, then rerun this script. Output: $whoami"
}
Write-Host "Authenticated as: $whoami" -ForegroundColor Green

if (-not $SkipDeploy) {
    Write-Step "Deploying backend to Fly"
    Set-Location (Join-Path $repoRoot $BackendDir)
    Invoke-Checked -FilePath $fly -Arguments @("deploy", "--config", "fly.toml", "--remote-only")
    Set-Location $repoRoot
}

Write-Step "Waiting for backend /ready (database connectivity)"
$readyDeadline = (Get-Date).AddSeconds(120)
$ready = $false
while ((Get-Date) -lt $readyDeadline -and -not $ready) {
    try {
        $readyResp = Invoke-RestMethod -Uri "$baseUrl/ready" -Method Get -TimeoutSec 10
        if ($readyResp.status -eq "ready") {
            $ready = $true
        }
    }
    catch {
        Start-Sleep -Seconds 5
    }
}
if (-not $ready) {
    throw "Backend did not become ready within 120s; database may still be starting."
}
Write-Host "Backend ready (database connected)." -ForegroundColor Green

if (-not $SkipMigrate) {
    Write-Step "Running production migrations"
    Invoke-Checked -FilePath $fly -Arguments @(
        "ssh", "console", "-a", $AppName, "-C", "sh -lc 'cd /app && alembic upgrade head'"
    )
}

Write-Step "Checking production health"
$health = Invoke-RestMethod -Uri "$baseUrl/health" -Method Get -TimeoutSec 20
if ($health.status -ne "ok") {
    throw "Health check failed: status=$($health.status)"
}
Write-Host "Health OK." -ForegroundColor Green

Write-Step "Checking CORS preflight"
$origins = @(
    "https://football-iq.pages.dev",
    "https://football-iq-aahmadf123.pages.dev",
    "https://football-iq.vercel.app",
    "https://football-iq-rho.vercel.app"
)
$corsResults = @()
foreach ($origin in $origins) {
    $r = Test-CorsPreflight -BaseUrl $baseUrl -Origin $origin
    $corsResults += $r
    Write-Host "origin=$($r.Origin) status=$($r.Status) allowOrigin=$($r.AllowOrigin)" -ForegroundColor Yellow
}
if ($corsResults.Where({ -not $_.Passed }).Count -gt 0) {
    throw "One or more CORS checks failed."
}

Write-Step "Checking auth and core endpoints"
$email = "prod-go-" + [Guid]::NewGuid().ToString("N").Substring(0, 8) + "@example.com"
$password = "pw-123456"
$registerBody = @{ email = $email; password = $password; full_name = "Prod Go" } | ConvertTo-Json
try {
    Invoke-RestMethod -Uri "$baseUrl/api/v1/auth/register" -Method Post -ContentType "application/json" -Body $registerBody | Out-Null
}
catch {
    # registration may conflict if retried with same email; this script uses random email, so ignore only if conflict
    if (-not $_.ErrorDetails.Message -or $_.ErrorDetails.Message -notmatch "Email already registered") {
        throw
    }
}

$loginBody = @{ email = $email; password = $password } | ConvertTo-Json
$tokenResp = Invoke-RestMethod -Uri "$baseUrl/api/v1/auth/login" -Method Post -ContentType "application/json" -Body $loginBody
$token = [string]$tokenResp.access_token
if (-not $token) {
    throw "Login failed: missing access token."
}

$headers = @{ Authorization = "Bearer $token" }
$corePaths = @(
    "/api/v1/videos?limit=3",
    "/api/v1/jobs?limit=3",
    "/api/v1/self-scout/tendencies?limit=3"
)
foreach ($p in $corePaths) {
    $resp = Invoke-WebRequest -Uri ($baseUrl + $p) -Method Get -Headers $headers -UseBasicParsing -TimeoutSec 20
    if ($resp.StatusCode -ne 200) {
        throw "Core endpoint check failed: $p status=$($resp.StatusCode)"
    }
    Write-Host "path=$p status=200" -ForegroundColor Yellow
}

Write-Step "Production backend is healthy and auth-ready"
Write-Host "All deploy, migration, CORS, and API checks passed for $baseUrl" -ForegroundColor Green
