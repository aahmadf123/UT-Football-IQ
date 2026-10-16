<#
  Runner for the "FootballIQ Worker" scheduled task (see
  install-windows-worker.ps1). Loads the private config, decrypts the worker
  password with DPAPI, and keeps the worker alive: if it exits it is restarted
  after a short back-off. Output goes to a daily log, keeping 14 days.
#>
$ErrorActionPreference = "Stop"
$DataDir = Join-Path $env:LOCALAPPDATA "FootballIQ\worker"
$LogDir = Join-Path $DataDir "logs"
$config = Get-Content (Join-Path $DataDir "config.json") -Raw | ConvertFrom-Json

$secure = $config.worker_password_dpapi | ConvertTo-SecureString
$password = [System.Net.NetworkCredential]::new("", $secure).Password

$env:BACKEND_API_URL = $config.api_url
$env:WORKER_EMAIL = $config.worker_email
$env:WORKER_PASSWORD = $password
$env:WORKER_ID = "laptop-$($env:COMPUTERNAME.ToLower())"
$env:PIPELINE_DEVICE = "cuda"
$env:PROCESSING_PROFILE = $config.profile
$env:WORKER_PROFILES = "review,quick,detailed"
$env:MODEL_DETECT_PATH = $config.detector_path
$env:PIPELINE_STORAGE_TRANSPORT = "api"
$env:PYTHONUTF8 = "1"
$env:PYTHONUNBUFFERED = "1"
$env:PATH = "$($config.ffmpeg_dir);$env:PATH"

Get-ChildItem $LogDir -Filter "worker-*.log" -ErrorAction SilentlyContinue |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-14) } | Remove-Item -Force

$backoff = 5
while ($true) {
    $log = Join-Path $LogDir ("worker-{0:yyyy-MM-dd}.log" -f (Get-Date))
    $started = Get-Date
    Push-Location $config.worker_dir
    try {
        & $config.python __main__.py *>> $log
    } finally {
        Pop-Location
    }
    "$(Get-Date -Format o) worker exited with code $LASTEXITCODE; restarting in $backoff s" | Add-Content $log
    # A worker that ran for a while is healthy: reset the back-off. One that
    # dies immediately (bad password, API down) backs off up to 5 minutes.
    $backoff = if (((Get-Date) - $started).TotalMinutes -gt 5) { 5 } else { [Math]::Min($backoff * 2, 300) }
    Start-Sleep -Seconds $backoff
}
