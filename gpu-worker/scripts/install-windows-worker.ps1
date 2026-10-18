<#
.SYNOPSIS
  Install the Football-IQ processing worker on a Windows GPU laptop so it
  starts at logon, restarts if it crashes, and processes film uploaded to the
  hosted site without anyone opening a terminal.

.DESCRIPTION
  * Creates gpu-worker\.venv-gpu (Python 3.12 via uv) with CUDA 12.8 PyTorch,
    which RTX 50-series (Blackwell, sm_120) GPUs require.
  * Downloads the detector weights once.
  * Stores the worker account's password encrypted with Windows DPAPI (only
    this Windows user on this machine can decrypt it) in
    %LOCALAPPDATA%\FootballIQ\worker\config.json.
  * Registers the "FootballIQ Worker" scheduled task: runs at logon, hidden,
    on battery too, restarted every minute if it exits.

  Re-run at any time to change settings; it replaces the task in place and
  restarts a running worker so the new settings take effect.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File gpu-worker\scripts\install-windows-worker.ps1 `
    -ApiUrl https://footiq-api-edge.firas-azfar.workers.dev -WorkerEmail worker@toledo.example

.EXAMPLE
  # Stop and remove the task (keeps config, logs and the venv)
  powershell -ExecutionPolicy Bypass -File gpu-worker\scripts\install-windows-worker.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [string]$ApiUrl = "https://footiq-api-edge.firas-azfar.workers.dev",
    [string]$WorkerEmail,
    # For unattended installs; otherwise the password is prompted for.
    [SecureString]$WorkerPassword,
    [ValidateSet("quick", "detailed")]
    [string]$ProcessingProfile = "detailed",
    [switch]$SkipInstall,
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$TaskName = "FootballIQ Worker"
$WorkerDir = Split-Path -Parent $PSScriptRoot
$DataDir = Join-Path $env:LOCALAPPDATA "FootballIQ\worker"
$ConfigPath = Join-Path $DataDir "config.json"
$Runner = Join-Path $PSScriptRoot "run-windows-worker.ps1"

function Stop-Worker {
    # Stopping the task ends the runner but can orphan its python child, and
    # "-MultipleInstances IgnoreNew" would keep a still-running runner on its
    # old config. End the runner and its whole process tree first.
    $all = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)
    $tree = @($all | Where-Object { $_.CommandLine -like "*run-windows-worker.ps1*" -and $_.ProcessId -ne $PID })
    $frontier = $tree
    while ($frontier.Count -gt 0) {
        $parents = $frontier | ForEach-Object { $_.ProcessId }
        $frontier = @($all | Where-Object { $parents -contains $_.ParentProcessId -and $tree.ProcessId -notcontains $_.ProcessId })
        $tree += $frontier
    }
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    foreach ($proc in $tree) {
        Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
    }
}

if ($Uninstall) {
    Stop-Worker
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Removed scheduled task '$TaskName'."
    return
}

New-Item -ItemType Directory -Force -Path $DataDir, (Join-Path $DataDir "logs"), (Join-Path $DataDir "models") | Out-Null

# ── 1. Python environment ────────────────────────────────────────────────────
$Python = Join-Path $WorkerDir ".venv-gpu\Scripts\python.exe"
if (-not $SkipInstall) {
    $uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
    if (-not $uv) {
        Write-Host "Installing uv (Python environment manager)..."
        py -m pip install --user --quiet uv
        $uv = (py -c "import sysconfig,os;print(os.path.join(sysconfig.get_path('scripts','nt_user'),'uv.exe'))").Trim()
    }
    if (-not (Test-Path $Python)) {
        & $uv venv --python 3.12 (Join-Path $WorkerDir ".venv-gpu")
    }
    Write-Host "Installing CUDA PyTorch and worker dependencies (one time, ~3 GB)..."
    & $uv pip install --quiet --python $Python torch==2.8.0 torchvision==0.23.0 --index-url https://download.pytorch.org/whl/cu128
    & $uv pip install --quiet --python $Python -r (Join-Path $WorkerDir "requirements.txt") "numpy==1.26.4"
}
& $Python -c "import torch,sys; ok=torch.cuda.is_available(); print('GPU:', torch.cuda.get_device_name(0) if ok else 'none (CPU fallback)'); sys.exit(0)"

# ── 2. ffmpeg ────────────────────────────────────────────────────────────────
$ffprobe = (Get-Command ffprobe -ErrorAction SilentlyContinue).Source
if (-not $ffprobe) {
    $ffprobe = Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages" -Recurse -Filter ffprobe.exe -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty FullName
}
if (-not $ffprobe) {
    Write-Host "Installing ffmpeg..."
    winget install --id Gyan.FFmpeg -e --silent --accept-source-agreements --accept-package-agreements | Out-Null
    $ffprobe = Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages" -Recurse -Filter ffprobe.exe | Select-Object -First 1 -ExpandProperty FullName
}
$FfmpegDir = Split-Path -Parent $ffprobe

# ── 3. Detector weights ──────────────────────────────────────────────────────
$Detector = Join-Path $DataDir "models\yolov8m.pt"
if (-not (Test-Path $Detector)) {
    Write-Host "Downloading detector weights..."
    Push-Location (Join-Path $DataDir "models")
    try { & $Python -c "from ultralytics import YOLO; YOLO('yolov8m.pt')" | Out-Null } finally { Pop-Location }
}

# ── 4. Private config (password DPAPI-encrypted) ─────────────────────────────
$existing = if (Test-Path $ConfigPath) { Get-Content $ConfigPath -Raw | ConvertFrom-Json } else { $null }
if (-not $WorkerEmail -and $existing) { $WorkerEmail = $existing.worker_email }
if (-not $WorkerEmail) { $WorkerEmail = Read-Host "Worker account email" }
$secret = if ($WorkerPassword) {
    $WorkerPassword | ConvertFrom-SecureString
} elseif ($existing -and -not $PSBoundParameters.ContainsKey("WorkerEmail")) {
    $existing.worker_password_dpapi
} else {
    Read-Host "Worker account password" -AsSecureString | ConvertFrom-SecureString
}
[ordered]@{
    api_url               = $ApiUrl.TrimEnd("/")
    worker_email          = $WorkerEmail
    worker_password_dpapi = $secret
    profile               = $ProcessingProfile
    detector_path         = $Detector
    ffmpeg_dir            = $FfmpegDir
    python                = $Python
    worker_dir            = $WorkerDir
} | ConvertTo-Json | Set-Content -Path $ConfigPath -Encoding UTF8

# ── 5. Scheduled task ────────────────────────────────────────────────────────
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$Runner`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
# The runner reads config.json once: restart it so new settings take effect.
# An interrupted job's lease expires and the server requeues it.
Stop-Worker
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "Processes Football-IQ film on this computer's GPU." -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName

Write-Host ""
Write-Host "Installed. The worker is running and will start automatically at every logon."
Write-Host "Logs: $(Join-Path $DataDir 'logs')"
