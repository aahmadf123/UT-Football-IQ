[CmdletBinding()]
param(
    [switch]$WithCloudflare,
    [switch]$SkipInstall,
    [switch]$UsePipelineStub,
    [int]$HealthTimeoutSeconds = 120,
    [int]$DockerStartupTimeoutSeconds = 180
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Write-Step {
    param([string]$Message)
    Write-Host "`n==> $Message" -ForegroundColor Cyan
}

function Test-CommandExists {
    param([string]$Name)
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

function Resolve-ToolPath {
    param(
        [string]$PrimaryName,
        [string[]]$FallbackPaths = @()
    )

    $cmd = Get-Command $PrimaryName -ErrorAction SilentlyContinue
    if ($cmd) {
        return $cmd.Source
    }

    foreach ($candidate in $FallbackPaths) {
        if (Test-Path $candidate) {
            return $candidate
        }
    }

    throw "Required command not found: $PrimaryName"
}

function Require-Command {
    param([string]$Name)
    if (-not (Test-CommandExists -Name $Name)) {
        throw "Required command not found: $Name"
    }
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

function Ensure-DockerEngine {
    param(
        [string]$DockerExe,
        [int]$TimeoutSeconds
    )

    try {
        & $DockerExe info | Out-Null
        if ($LASTEXITCODE -eq 0) {
            Write-Host "Docker engine is ready." -ForegroundColor Green
            return
        }
    }
    catch {
        # Fall through to start Docker Desktop.
    }

    $desktopExe = "$env:ProgramFiles\Docker\Docker\Docker Desktop.exe"
    if (Test-Path $desktopExe) {
        Write-Step "Starting Docker Desktop"
        Start-Process -FilePath $desktopExe | Out-Null
    }

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            & $DockerExe info | Out-Null
            if ($LASTEXITCODE -eq 0) {
                Write-Host "Docker engine is ready." -ForegroundColor Green
                return
            }
        }
        catch {
            # keep waiting
        }
        Start-Sleep -Milliseconds 1500
    }

    throw "Docker engine did not become ready within $TimeoutSeconds seconds."
}

function Wait-HttpOk {
    param(
        [string]$Url,
        [int]$TimeoutSeconds
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $resp = Invoke-WebRequest -Uri $Url -Method Get -UseBasicParsing -TimeoutSec 10
            if ($resp.StatusCode -ge 200 -and $resp.StatusCode -lt 300) {
                Write-Host "OK: $Url" -ForegroundColor Green
                return
            }
        }
        catch {
            Start-Sleep -Milliseconds 1000
        }
    }
    throw "Timed out waiting for healthy endpoint: $Url"
}

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Resolve-Path (Join-Path $scriptDir "..")
$python = Join-Path $repoRoot ".venv/Scripts/python.exe"

Write-Step "Repository root: $repoRoot"
Set-Location $repoRoot

Write-Step "Checking required tools"
Require-Command -Name "npm"
if (-not (Test-Path $python)) {
    throw "Python virtualenv not found at $python. Create it first (python -m venv .venv)."
}

$docker = Resolve-ToolPath -PrimaryName "docker" -FallbackPaths @(
    "$env:ProgramFiles\Docker\Docker\resources\bin\docker.exe",
    "$env:ProgramW6432\Docker\Docker\resources\bin\docker.exe"
)

$dockerDir = Split-Path -Parent $docker
if ($env:PATH -notlike "*$dockerDir*") {
    $env:PATH = "$dockerDir;$env:PATH"
}

Ensure-DockerEngine -DockerExe $docker -TimeoutSeconds $DockerStartupTimeoutSeconds

try {
    & $docker context use desktop-linux | Out-Null
}
catch {
    Write-Host "Docker context desktop-linux not available, using current context." -ForegroundColor Yellow
}

if (-not (Test-Path (Join-Path $repoRoot ".env"))) {
    Write-Step "No .env found. Copying from .env.example"
    Copy-Item (Join-Path $repoRoot ".env.example") (Join-Path $repoRoot ".env")
}

if (-not $SkipInstall) {
    Write-Step "Installing backend dependencies"
    Invoke-Checked -FilePath $python -Arguments @(
        "-m", "pip", "install",
        "-r", (Join-Path $repoRoot "backend/requirements.txt"),
        "-r", (Join-Path $repoRoot "backend/requirements-dev.txt")
    )

    Write-Step "Installing frontend dependencies"
    Set-Location (Join-Path $repoRoot "frontend")
    Invoke-Checked -FilePath "npm" -Arguments @("ci")
    Set-Location $repoRoot

    if ($WithCloudflare) {
        Write-Step "Installing workers dependencies"
        Set-Location (Join-Path $repoRoot "workers")
        Invoke-Checked -FilePath "npm" -Arguments @("ci")
        Set-Location $repoRoot
    }
}

if ($UsePipelineStub) {
    $env:PIPELINE_STUB = "1"
    Write-Step "PIPELINE_STUB=1 enabled for faster local wiring checks"
}

Write-Step "Starting Postgres"
Invoke-Checked -FilePath $docker -Arguments @("compose", "up", "-d", "db")

Write-Step "Applying migrations and seeding users"
Invoke-Checked -FilePath $docker -Arguments @("compose", "--profile", "migrate", "up", "migrate", "seed")

Write-Step "Starting backend, frontend, and gpu-worker"
Invoke-Checked -FilePath $docker -Arguments @("compose", "--profile", "pipeline", "up", "-d", "backend", "frontend", "gpu-worker")

Write-Step "Waiting for service health"
Wait-HttpOk -Url "http://localhost:8000/health" -TimeoutSeconds $HealthTimeoutSeconds
Wait-HttpOk -Url "http://localhost:3000" -TimeoutSeconds $HealthTimeoutSeconds

if ($WithCloudflare) {
    Write-Step "Starting Cloudflare Worker dev server in a new terminal"
    $workerDir = Join-Path $repoRoot "workers"
    $workerCmd = "Set-Location '$workerDir'; npm run dev"
    Start-Process -FilePath "pwsh" -ArgumentList @("-NoExit", "-Command", $workerCmd) | Out-Null
    Write-Host "Worker terminal started. Wrangler will run there." -ForegroundColor Green
}

Write-Step "Done"
Write-Host "Backend:   http://localhost:8000/health" -ForegroundColor Yellow
Write-Host "Frontend:  http://localhost:3000" -ForegroundColor Yellow
if ($WithCloudflare) {
    Write-Host "Worker:    http://127.0.0.1:8787 (wrangler dev default)" -ForegroundColor Yellow
}
Write-Host "Seed admin: admin@example.com / change-me-admin" -ForegroundColor Yellow
