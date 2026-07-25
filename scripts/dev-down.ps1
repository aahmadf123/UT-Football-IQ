[CmdletBinding()]
param()

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

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Resolve-Path (Join-Path $scriptDir "..")
Set-Location $repoRoot
$docker = Resolve-DockerPath
$dockerDir = Split-Path -Parent $docker
if ($env:PATH -notlike "*$dockerDir*") {
    $env:PATH = "$dockerDir;$env:PATH"
}

Write-Host "Stopping Football-IQ local stack..." -ForegroundColor Cyan
& $docker compose --profile pipeline down
if ($LASTEXITCODE -ne 0) {
    throw "Failed to stop compose services."
}

Write-Host "Stopped. If a Wrangler terminal is open, close it manually." -ForegroundColor Green
