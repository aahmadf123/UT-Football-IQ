# Run the processing worker on your own computer

Football-IQ processes film on the local CPU. Windows and macOS laptops do not
need a dedicated GPU, a GPU rental, or object-store credentials. The native
launcher starts the local worker with the film room; see the
[local runbook](../runbook-local.md) for installation and native packaging.
This guide covers running the worker directly from a checkout and connecting it
to either a local API or the existing team API, including Cloudflare.

Pressing **Process Film** queues a job. A connected worker claims it, reports
progress, and saves results. Queue priority (`same_session`/`nightly`) and the
processing profile (`review`/`quick`/`detailed`) are separate choices. Nightly
maintenance can enqueue eligible training; it is not a cloud inference service.

Recordings from different angles, heights, cameras, and start times are welcome.
Group them into one session. Each recording remains independently playable and
processable; manual alignment is optional. Automatic cross-view identity,
synchronized camera rigs, and 3D reconstruction are not established capabilities.

## Install the CPU environment

Use Python **3.11** for the worker in its own environment, separate from the
backend environment. Install `ffmpeg` and `ffprobe` with `libx264` encoding on
`PATH`. Review the dependency terms in [LICENSES.md](../../LICENSES.md).

Windows PowerShell, from the repository root:

```powershell
cd gpu-worker
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements-cpu.txt
```

macOS/Linux:

```bash
cd gpu-worker
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-cpu.txt
```

The pinned CPU requirements select Torch 2.8.0 with torchvision 0.23.0 on
Windows/Linux CPU and Apple Silicon. Torch 2.6 or later is required for the
`torch.load` security fix, and PyTorch publishes no such wheels for Intel Macs,
so a native Intel Mac install fails; run the worker in the Linux CPU container
(`gpu-worker/Dockerfile`) there instead. NumPy 1.26.4, SciPy 1.14.1, and OpenCV
4.10.0.84 keep the image/tracker bridge compatible. Installing
dependencies is a one-time network operation. Optional MMPose, NVIDIA TAO, and
SAM model stacks are not required for review or basic player tracking.

Quick processing needs actual local player weights. The native launcher's
model-install action downloads YOLOv8n to its data directory. In a source worker
environment, after reviewing the Ultralytics AGPL/commercial licensing options,
an explicit one-time download is:

```bash
python -c "from ultralytics import YOLO; YOLO('yolov8n.pt')"
```

Set `MODEL_DETECT_PATH` to the resulting file's absolute path. Weights stay out of
Git. Once dependencies and weights are installed, local film review and supported
CV run without network access; connecting to a hosted backend requires network.

## Profiles and model readiness

| Profile/capability | Actual behavior | Required input |
|---|---|---|
| **Review** | Imports film, proposes boundaries, creates clean H264 play media; staff can edit cuts and annotations | Decodable recording, FFmpeg/ffprobe |
| **Quick** | Adds full-frame player detection, IoU tracking, calibration checks, candidate events/labels, and annotated exports | Real local player weights; interpretation can abstain |
| **Detailed** | Adds optional pose, advanced analysis, and HLS stages to Quick | Relevant real models and usable evidence; unavailable stages skip |
| Play boundaries | Optical-flow proposals for review | No trained segmentation model; camera motion can confuse boundaries |
| Player tracks | Actual detector boxes with within-recording association | Generic YOLO person weights are a baseline, not Toledo football validation |
| Ball | Disabled/unavailable until dedicated real ball weights are configured | `MODEL_BALL_PATH` and `DETECT_BALL_ENABLED=1`; generic COCO sports-ball detections are insufficient |
| Identity | Jersey OCR/optional appearance candidates with roster context; rosterless runs skip | Roster and readable jerseys; staff verification before player attribution |
| Pose | Per-player crops with observed keypoints | `MODEL_POSE_PATH` plus compatible optional runtime; missing model skips |
| Coverage learning | Real local NumPy head fitting, persistent candidate artifact, measured held-out predictions | Human labels and matching real spatial features; see [learning guide](../cv/local-cpu-learning.md) |

Review keeps a source-wide annotation window when no usable play cuts exist,
including a recording shorter than three seconds. This window has unconfirmed
play-boundary confidence and remains available for staff to edit.

No production stage silently substitutes synthetic players or skeletons. Explicit
test stubs retain `synthetic_test` provenance and cannot become trusted analysis.
The worker records actual model/device/hash provenance; requested router names
alone are not evidence that those models ran.

`PIPELINE_DEVICE=cpu` is the default. CUDA and MPS are explicit local opt-ins;
availability does not establish model compatibility or performance. Mac
acceleration must be tested on the target hardware/model. Start with bounded
threads and Quick on a short representative clip. Compare processing elapsed
time, memory, playback responsiveness, and track continuity before choosing
stride, tiling, or Detailed. There is no fixed laptop processing-time guarantee.

## Connect to the backend

Use a worker account with analyst permissions. A local development seed creates
one via `python -m scripts.seed_users` in the backend environment. For the hosted
team API, an administrator creates/promotes the account in Settings. Keep worker
credentials in private runtime configuration, outside Git.

PowerShell example:

```powershell
$env:BACKEND_API_URL = "http://localhost:8000" # or your existing team API URL
$env:WORKER_EMAIL = "worker@yourteam.example"
$env:WORKER_PASSWORD = "<worker password>"
$env:PIPELINE_DEVICE = "cpu"
$env:PIPELINE_CPU_THREADS = "2"
$env:PROCESSING_PROFILE = "quick"
$env:MODEL_DETECT_PATH = "C:\Football-IQ-data\models\yolov8n.pt"
$env:PIPELINE_STORAGE_TRANSPORT = "api"
python __main__.py
```

macOS/Linux example:

```bash
export BACKEND_API_URL=http://localhost:8000
export WORKER_EMAIL=worker@yourteam.example
export WORKER_PASSWORD='<worker password>'
export PIPELINE_DEVICE=cpu PIPELINE_CPU_THREADS=2 PROCESSING_PROFILE=quick
export MODEL_DETECT_PATH=/absolute/path/to/yolov8n.pt
export PIPELINE_STORAGE_TRANSPORT=api
python __main__.py
```

API media transport authenticates downloads/uploads through the backend. This
works with server-side local disk or R2/S3 and keeps bucket credentials on the
server. Direct storage transport remains compatible with existing installations:
set `PIPELINE_STORAGE_TRANSPORT=direct` and configure the same shared
`LOCAL_STORAGE_ROOT` or `STORAGE_BACKEND=s3`/`S3_*` bucket mapping as the backend.
These are explicit alternatives; local processing itself needs no provider.

The standalone CLI saves local artifacts without an API:

```bash
python -m pipeline run --input /absolute/path/to/film.mp4 \
  --profile review --device cpu --no-backend --out /absolute/path/to/results
```

Set `PIPELINE_STORAGE_TRANSPORT=direct` for an offline CLI if the shell was
previously configured for API media transport. Select `--profile quick` after
installing player weights; `--stride 5` samples fewer source frames at the cost
of tracking detail. The worker reads environment variables; it does not load the
repository's `.env` automatically.

## Metrics and interpretation limits

Source presentation timestamps are preserved, including variable-frame-rate
phone/edited footage. Clean CFR playback proxies carry source-frame/time mapping;
event timing and track derivatives use actual observed elapsed time. Clip playback
is separate from annotated exports so overlay toggles remain functional.

Field coordinates require safe time-indexed calibration. Failed or incomplete
mapping yields absent coordinates, not zeroes. Calibration self-checks are not
independent yard-measurement validation. Tracking speed/distance remain reviewable
experimental measurements until supported by the clip's calibration, source,
identity, and evaluation evidence. Untrusted numbers are suppressed from trusted
reports and overlays. Missing snap/throw observations cannot produce time to
throw; missing snap prevents route/coverage interpretation.

Biomechanical and advanced football metric switches remain off by default
(`POSE_EXPERIMENTAL_METRICS=0`, `EXPERIMENTAL_FOOTBALL_METRICS=0`). They do not
establish injury risk, contact force, validated workload, assignment grades, or
metric accuracy from arbitrary camera views. Human corrections save durable
annotation revisions; they are not automatic proof of model quality. Candidate
coverage training requires session-disjoint evaluation, exact artifact hashes,
and explicit administrator promotion. Cross-regime image/label distillation is
quarantined because its old image/label pairing was invalid.

## Verify and recover

Upload a short recording, select Review, and confirm a clean playable clip. Then
run Quick with installed player weights and inspect actual provenance, boxes,
stage results, and the review queue. A partial job can still have useful playable
film; a skipped optional model should explain what is unavailable.

- **Queued:** confirm the worker is running, its API URL/login are correct, and
  its account can claim jobs. The configured poll interval is normally two seconds.
- **Model unavailable:** install/select actual weights, or use Review. Installing
  the Python package alone does not install a Toledo-trained model.
- **Download/upload failed:** verify API transport/login or the direct shared
  storage configuration. A hosted R2 backend does not require giving laptop users
  its bucket keys when API transport is selected.
- **Interrupted processing:** checkpoints and idempotent writes allow compatible
  work to resume; source/model/annotation changes invalidate affected work.
  Pause/cancel and lease fencing prevent stale workers from continuing writes.
- **Metrics absent:** inspect calibration/events/identity readiness and review the
  observations. Missing evidence should remain missing instead of generating a
  plausible number.
