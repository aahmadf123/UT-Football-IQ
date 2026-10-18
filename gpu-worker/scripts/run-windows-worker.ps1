<#
  Runner for the "FootballIQ Worker" scheduled task (see
  install-windows-worker.ps1). Loads the private config, decrypts the worker
  password with DPAPI, and keeps the worker alive: if it exits it is restarted
  after a short back-off. Output goes to a daily log (rotated while the
  worker runs), keeping 14 days.
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
# GPU laptop: BoT-SORT (Kalman + camera-motion compensation) for full-quality
# jobs. On Toledo drone film it halved track fragmentation vs the IoU tracker.
$env:ENABLE_BOTSORT_NIGHTLY = "1"
$env:PROCESSING_PROFILE = $config.profile
$env:WORKER_PROFILES = "review,quick,detailed"
$env:MODEL_DETECT_PATH = $config.detector_path
$env:PIPELINE_STORAGE_TRANSPORT = "api"
$env:PYTHONUTF8 = "1"
$env:PYTHONUNBUFFERED = "1"
$env:PATH = "$($config.ffmpeg_dir);$env:PATH"

function Remove-OldLogs {
    Get-ChildItem $LogDir -Filter "worker-*.log" -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-14) } | Remove-Item -Force
}

function Get-LogPath {
    Join-Path $LogDir ("worker-{0:yyyy-MM-dd}.log" -f (Get-Date))
}

$backoff = 5
while ($true) {
    Remove-OldLogs
    $script:currentLog = Get-LogPath
    $started = Get-Date
    Push-Location $config.worker_dir
    # Native stderr must not become a terminating error under "Stop".
    $ErrorActionPreference = "Continue"
    try {
        # The worker polls forever, so pick the daily file per line (and prune
        # old files when the day changes) instead of once per worker start.
        & $config.python __main__.py 2>&1 | ForEach-Object {
            $path = Get-LogPath
            if ($path -ne $script:currentLog) {
                $script:currentLog = $path
                Remove-OldLogs
            }
            "$_" | Add-Content -Path $path
        }
    } finally {
        $ErrorActionPreference = "Stop"
        Pop-Location
    }
    "$(Get-Date -Format o) worker exited with code $LASTEXITCODE; restarting in $backoff s" | Add-Content (Get-LogPath)
    # A worker that ran for a while is healthy: reset the back-off. One that
    # dies immediately (bad password, API down) backs off up to 5 minutes.
    $backoff = if (((Get-Date) - $started).TotalMinutes -gt 5) { 5 } else { [Math]::Min($backoff * 2, 300) }
    Start-Sleep -Seconds $backoff
}
