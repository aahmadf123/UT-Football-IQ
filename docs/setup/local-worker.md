# Run the processing worker on your own computer

Film does **not** process itself, and nothing about processing waits for
midnight. When you press **Process Film**, the backend queues a job; the job
runs the moment a **worker** — the `gpu-worker/` program — claims it. The
nightly 08:00 UTC cron only does maintenance (lease sweeps, corrections
export, training triggers); it never runs film jobs.

So to get film processed, exactly one thing has to be true: a worker is
running somewhere, pointed at your backend. This guide runs it on your own
PC. A CUDA-capable NVIDIA GPU makes it much faster, but plain CPU works —
expect minutes per clip on CPU instead of seconds.

```
Your PC (gpu-worker) ──claim/heartbeat/writeback──► Cloudflare Worker ──► FastAPI container
        │                                                                        │
        └───────────────── download video / upload results ──► R2 ◄──────────────┘
```

## 1. One-time: install

Python 3.11+ recommended. From the repo root:

```powershell
cd gpu-worker
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install torch torchvision            # or the CUDA build for your GPU — see pytorch.org
pip install -r requirements.txt
```

(macOS/Linux: `source .venv/bin/activate` instead.)

Ultralytics downloads YOLO weights on first run; allow outbound internet once.

## 2. One-time: create the worker's service account

The worker logs into the backend like any staff user. Create an account for it
and promote it to `analyst` (any admin token works for the PATCH):

```powershell
$api = "https://<your-worker>.workers.dev"

# Register the account (new accounts start as viewer)
Invoke-RestMethod -Method Post -Uri "$api/api/v1/auth/register" -ContentType "application/json" -Body (@{
  email = "worker@yourteam.example"; password = "<strong password>"; full_name = "Processing Worker"
} | ConvertTo-Json)

# Log in as YOUR admin account to get a token
$tok = (Invoke-RestMethod -Method Post -Uri "$api/api/v1/auth/login" -ContentType "application/json" -Body (@{
  email = "<your admin email>"; password = "<your password>"
} | ConvertTo-Json)).access_token

# Find the worker account's id, then promote it
$users = Invoke-RestMethod -Uri "$api/api/v1/auth/users" -Headers @{ Authorization = "Bearer $tok" }
$workerId = ($users | Where-Object email -eq "worker@yourteam.example").id
Invoke-RestMethod -Method Patch -Uri "$api/api/v1/auth/users/$workerId/role" -ContentType "application/json" `
  -Headers @{ Authorization = "Bearer $tok" } -Body (@{ role = "analyst" } | ConvertTo-Json)
```

## 3. Every run: environment + start

The worker needs the backend URL, its login, and the same R2 credentials the
container uses (the worker downloads raw video and uploads results directly —
bytes never route through the container). Values marked `footiq-*` must match
the `R2_BUCKET_*` vars in `workers/api-edge/wrangler.jsonc`.

```powershell
cd gpu-worker
.venv\Scripts\Activate.ps1

$env:BACKEND_API_URL       = "https://<your-worker>.workers.dev"
$env:WORKER_EMAIL          = "worker@yourteam.example"
$env:WORKER_PASSWORD       = "<strong password>"

$env:STORAGE_BACKEND       = "s3"
$env:S3_ENDPOINT_URL       = "https://<account-id>.r2.cloudflarestorage.com"
$env:S3_ACCESS_KEY_ID      = "<R2 access key id>"
$env:S3_SECRET_ACCESS_KEY  = "<R2 secret access key>"
# Logical→physical bucket mapping (DB URIs say raw-video/clips/…; R2
# provisions the prefixed names):
$env:S3_BUCKET_RAW         = "footiq-raw-video"
$env:S3_BUCKET_CLIPS       = "footiq-clips"
$env:S3_BUCKET_OVERLAYS    = "footiq-overlays"
$env:S3_BUCKET_ARTIFACTS   = "footiq-artifacts"

python __main__.py
```

Keep the window open. The worker polls every ~10 s, claims queued jobs
(`FOR UPDATE SKIP LOCKED` server-side, so several workers never collide),
heartbeats a lease, and writes per-stage progress into the job row — the Film
Room's **Pipeline jobs** card shows it live. Ctrl+C shuts down gracefully.

## 4. Verify

1. Upload film in the app and press **Process Film** — the row shows **Queued**.
2. Within ~10 s the worker log prints `processing_job`, and the row flips to
   **Processing** with per-stage progress.
3. On success the video flips to **Processed** and clips appear in Browse Film.

## Troubleshooting

- **Job stays Queued forever** — no worker is connected. Check the worker
  window is running and `BACKEND_API_URL` is right; a login failure in the log
  means `WORKER_EMAIL`/`WORKER_PASSWORD` are wrong or the account was never
  promoted past `viewer`.
- **`NoSuchBucket` in the worker log** — the `S3_BUCKET_*` mapping vars are
  missing or don't match the deployed bucket names.
- **Everything fails at download** — R2 credentials/endpoint are wrong, or the
  R2 token lacks read/write on the four buckets.
- **Worker crashed mid-job** — nothing is lost: the lease expires, the sweeper
  requeues the job, and the next claim picks it up.
