[CmdletBinding()]
param(
    [switch]$WithCloudflare
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Resolve-DockerPath {
    $cmd = Get-Command docker -ErrorAction SilentlyContinue
    if ($cmd) {
        return $cmd.Source
    }

    $fallbacks = @(
        "$env:ProgramFiles\Docker\Docker\resources\bin\docker.exe",
        "$env:ProgramW6432\Docker\Docker\resources\bin\docker.exe"
    )
    foreach ($candidate in $fallbacks) {
        if (Test-Path $candidate) {
            return $candidate
        }
    }

    throw "Required command not found: docker"
}

function Add-Result {
    param(
        [System.Collections.Generic.List[object]]$Rows,
        [string]$Check,
        [bool]$Passed,
        [string]$Details
    )

    $status = if ($Passed) { "PASS" } else { "FAIL" }
    $Rows.Add([pscustomobject]@{
        Check = $Check
        Status = $status
        Details = $Details
    }) | Out-Null
}

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Resolve-Path (Join-Path $scriptDir "..")
Set-Location $repoRoot

$rows = New-Object System.Collections.Generic.List[object]
$failed = $false

$docker = Resolve-DockerPath
$dockerDir = Split-Path -Parent $docker
if ($env:PATH -notlike "*$dockerDir*") {
    $env:PATH = "$dockerDir;$env:PATH"
}

try {
    & $docker info | Out-Null
    Add-Result -Rows $rows -Check "Docker engine" -Passed $true -Details "Engine reachable"
}
catch {
    Add-Result -Rows $rows -Check "Docker engine" -Passed $false -Details $_.Exception.Message
    $failed = $true
}

try {
    $psOut = & $docker compose ps 2>&1
    $text = $psOut | Out-String
    if ($text -match "backend" -and $text -match "frontend" -and $text -match "db") {
        Add-Result -Rows $rows -Check "Compose services" -Passed $true -Details "Core services listed"
    }
    else {
        Add-Result -Rows $rows -Check "Compose services" -Passed $false -Details "Expected services not all present"
        $failed = $true
    }
}
catch {
    Add-Result -Rows $rows -Check "Compose services" -Passed $false -Details $_.Exception.Message
    $failed = $true
}

try {
    $health = Invoke-RestMethod -Uri "http://localhost:8000/health" -Method Get -TimeoutSec 10
    $ok = ($health.status -eq "ok")
    Add-Result -Rows $rows -Check "Backend health" -Passed $ok -Details (("status={0}" -f $health.status))
    if (-not $ok) { $failed = $true }
}
catch {
    Add-Result -Rows $rows -Check "Backend health" -Passed $false -Details $_.Exception.Message
    $failed = $true
}

try {
    $resp = Invoke-WebRequest -Uri "http://localhost:3000" -Method Get -UseBasicParsing -TimeoutSec 10
    $ok = ($resp.StatusCode -ge 200 -and $resp.StatusCode -lt 400)
    Add-Result -Rows $rows -Check "Frontend HTTP" -Passed $ok -Details (("status={0}" -f $resp.StatusCode))
    if (-not $ok) { $failed = $true }
}
catch {
    Add-Result -Rows $rows -Check "Frontend HTTP" -Passed $false -Details $_.Exception.Message
    $failed = $true
}

try {
    $alembicCurrent = & $docker compose exec -T backend alembic current 2>&1 | Out-String
    $ok = $alembicCurrent -match "\(head\)"
    Add-Result -Rows $rows -Check "Migrations" -Passed $ok -Details ($alembicCurrent.Trim() -replace "\s+", " ")
    if (-not $ok) { $failed = $true }
}
catch {
    Add-Result -Rows $rows -Check "Migrations" -Passed $false -Details $_.Exception.Message
    $failed = $true
}

try {
    $ps = & $docker compose ps gpu-worker 2>&1 | Out-String
    $ok = $ps -match "Up"
    Add-Result -Rows $rows -Check "GPU worker" -Passed $ok -Details "Container status inspected"
    if (-not $ok) { $failed = $true }
}
catch {
    Add-Result -Rows $rows -Check "GPU worker" -Passed $false -Details $_.Exception.Message
    $failed = $true
}

if ($WithCloudflare) {
    try {
        $resp = Invoke-WebRequest -Uri "http://127.0.0.1:8787" -Method Get -UseBasicParsing -TimeoutSec 8
        $ok = ($resp.StatusCode -ge 200 -and $resp.StatusCode -lt 500)
        Add-Result -Rows $rows -Check "Cloudflare worker dev" -Passed $ok -Details (("status={0}" -f $resp.StatusCode))
        if (-not $ok) { $failed = $true }
    }
    catch {
        $statusCode = $null
        if ($_.Exception.Response -and $_.Exception.Response.StatusCode) {
            $statusCode = [int]$_.Exception.Response.StatusCode
        }
        if ($statusCode -and $statusCode -ge 200 -and $statusCode -lt 500) {
            Add-Result -Rows $rows -Check "Cloudflare worker dev" -Passed $true -Details (("status={0}" -f $statusCode))
        }
        else {
            Add-Result -Rows $rows -Check "Cloudflare worker dev" -Passed $false -Details $_.Exception.Message
            $failed = $true
        }
    }
}

$rows | Format-Table -AutoSize

if ($failed) {
    exit 1
}

Write-Host "All checks passed." -ForegroundColor Green
