# Deploy the Football-IQ GPU worker to Fly.io.
# Required environment variables:
#   CLOUDFLARE_API_TOKEN      - the Cloudflare API token value
#   CLOUDFLARE_API_TOKEN_ID   - the token ID shown in the Cloudflare dashboard
#   WORKER_PASSWORD           - password for worker@football-iq.fly.dev

$ErrorActionPreference = "Stop"

$App = "football-iq-gpu-worker"
$BackendApp = if ($env:BACKEND_APP) { $env:BACKEND_APP } else { "football-iq-backend" }
$AccountId = "4607053406491c18132943949b972140"

if (-not $env:CLOUDFLARE_API_TOKEN) { throw "CLOUDFLARE_API_TOKEN is required" }
if (-not $env:CLOUDFLARE_API_TOKEN_ID) { throw "CLOUDFLARE_API_TOKEN_ID is required" }
if (-not $env:WORKER_PASSWORD) { throw "WORKER_PASSWORD is required" }

$sha = [System.Security.Cryptography.SHA256]::Create()
$bytes = [System.Text.Encoding]::UTF8.GetBytes($env:CLOUDFLARE_API_TOKEN)
$R2_SECRET_ACCESS_KEY = ([System.BitConverter]::ToString($sha.ComputeHash($bytes)) -replace '-', '').ToLowerInvariant()
$R2_ACCESS_KEY_ID = $env:CLOUDFLARE_API_TOKEN_ID

$backendSecrets = [System.IO.Path]::GetTempFileName()
@"
SEED_WORKER_EMAIL=worker@football-iq.fly.dev
SEED_WORKER_PASSWORD=$env:WORKER_PASSWORD
SEED_USERS_ON_STARTUP=true
SEED_RESET_PASSWORDS=true
"@ | Set-Content -Path $backendSecrets -Encoding UTF8
Write-Host "Syncing worker password on backend $BackendApp (SEED_RESET_PASSWORDS=true)..."
Get-Content $backendSecrets | flyctl secrets import -a $BackendApp

@"
SEED_RESET_PASSWORDS=false
"@ | Set-Content -Path $backendSecrets -Encoding UTF8
Write-Host "Disabling password reset on backend $BackendApp..."
Get-Content $backendSecrets | flyctl secrets import -a $BackendApp
Remove-Item -Path $backendSecrets -Force

Write-Host "Creating GPU worker app $App (if it doesn't exist)..."
flyctl apps create $App --org personal; if ($LASTEXITCODE -ne 0) { Write-Host "App may already exist, continuing..." }

$gpuSecrets = [System.IO.Path]::GetTempFileName()
@"
WORKER_EMAIL=worker@football-iq.fly.dev
WORKER_PASSWORD=$env:WORKER_PASSWORD
R2_ACCESS_KEY_ID=$R2_ACCESS_KEY_ID
R2_SECRET_ACCESS_KEY=$R2_SECRET_ACCESS_KEY
R2_ENDPOINT_URL=https://${AccountId}.r2.cloudflarestorage.com
STORAGE_BACKEND=r2
"@ | Set-Content -Path $gpuSecrets -Encoding UTF8
Write-Host "Setting GPU worker secrets..."
Get-Content $gpuSecrets | flyctl secrets import -a $App
Remove-Item -Path $gpuSecrets -Force

Write-Host "Deploying $App..."
flyctl deploy -a $App
