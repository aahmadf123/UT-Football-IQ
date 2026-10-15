# Football-IQ

Football-IQ is a local American-football film room and computer-vision platform
for the Toledo Rockets. Import practice or game film from any angle or height,
group multiple recordings into a session, review plays, and correct observations.
Processing defaults to the computer's CPU; cloud GPUs are optional external
infrastructure, not a dependency.

Film review works without inference models. A configured real player detector
adds candidate tracks; football events, identity, field measurements, and tactical
labels have explicit review and validation limits. Human corrections can train a
local coverage candidate, with independent evaluation and administrator promotion
before serving. The repository does not establish Hudl/SkillCorner accuracy or
automatic tactical tagging on arbitrary footage.

## Service map

| Service | Role | Tech |
|---------|------|------|
| **Frontend** | Coach-facing web UI: dashboard, film room, clip review with overlays, scouting, analytics | Next.js 16 (static export), React 19, TypeScript |
| **Backend API** | REST API: auth (JWT), videos, clips, the job queue (claim/lease/heartbeat), corrections, storage facade + signed streaming, nightly scheduler | FastAPI, Python 3.12, SQLAlchemy |
| **Database** | Primary data store, pgvector similarity index, and the **job queue** (`processing_jobs` with `FOR UPDATE SKIP LOCKED` leases) | PostgreSQL 16 + pgvector |
| **Pipeline worker** | Claims jobs, prepares clean playback, runs selected local CV stages, checkpoints work and writes results through the API | Python 3.11; CPU-first YOLO + IoU; optional pose/acceleration |
| **Storage** | `local://` disk (default) or `s3://` buckets `raw-video` / `clips` / `overlays` / `artifacts` — one `STORAGE_BACKEND` switch | Local volume or any S3-compatible object store |

There is no message broker. The backend's `processing_jobs` table *is* the
queue. For hosted deployments, `workers/api-edge/` fronts the backend as a
Cloudflare Worker (R2 multipart uploads + signed streaming at the edge, the
FastAPI app as a Cloudflare Container, a cron trigger for the nightly tick);
the frontend deploys to Vercel. See `docs/setup/cloudflare.md` and
`docs/setup/vercel.md`.

Hosted processing now runs a supervised CPU pipeline alongside the API in that
same Cloudflare Container, with separate Python environments. It uses a dedicated
staff service account and a checksum-pinned pretrained person detector; no separate
GPU or CPU hosting provider is required. Uploaded film and results live in the
shared database/R2 library, and verified content hashes prevent duplicate recording
rows across uploads and staff accounts. Local libraries remain supported.

The film workspace includes frame-synchronized player boxes, identity/trail layers,
a player inspector and a seekable play timeline. Tracking uses the original
recording clock even when reviewing a later clip or a resized playback proxy.
CFBD data stays server-side: administrators can refresh season context and weekly
play-by-play from **Scouting → College Data** after configuring `CFBD_API_KEY`.

## Quick start (local)

The native launcher starts a local database, API, UI, and CPU worker together.
See [the local runbook](docs/runbook-local.md) for source setup, native packaging,
and connecting the laptop to an existing team backend, including Cloudflare.
Native distributions must be built and checked on their target operating system.

For manual development, use separate environments: Python 3.12 for the backend,
Python 3.11 for the CPU worker, Node 20+, PostgreSQL 16 with `pgvector`, and
`ffmpeg` **and** `ffprobe` on `PATH` (H264 encoding requires `libx264`).

```bash
cp .env.example backend/.env  # edit local database/login and development secrets
```

**1. Database**

```bash
createdb footiq
psql footiq -c 'CREATE EXTENSION IF NOT EXISTS vector;'
```

**2. Backend**

```bash
cd backend
python3.12 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
alembic upgrade head
python -m scripts.seed_users        # admin + gpu-worker service account
uvicorn app.main:app --reload --port 8000
```

**3. Pipeline worker** (separate shell)

```bash
cd gpu-worker
python3.11 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements-cpu.txt
BACKEND_API_URL=http://localhost:8000 PIPELINE_STORAGE_TRANSPORT=api \
WORKER_EMAIL=worker@example.com WORKER_PASSWORD=change-me-worker \
PIPELINE_DEVICE=cpu MODEL_DETECT_PATH=/absolute/path/to/yolov8n.pt python __main__.py
```

**4. Frontend** (separate shell)

```bash
cd frontend
npm ci
npm run dev
```

Open `http://localhost:3000`, sign in with the configured development account,
create a session, add recordings, and select Review, Quick, or Detailed processing.
Configure actual model weights before Quick processing; the installation and
license steps are in [the worker guide](docs/setup/local-worker.md). Review
processing prepares film without a detection model. Progress, unavailable
capabilities, and partial analysis are shown in the film room. Development
example credentials must be changed for a shared installation.

**No services at all** — run the pipeline directly on any video file:

```bash
cd gpu-worker
python -m pipeline run --input "../Drone Footage/Dji 20260416111539 0284 D.mp4" \
  --profile review --device cpu --no-backend --out ./out
```

Use `--profile quick` after selecting local player weights. This command writes
artifacts to disk; saving sessions, review corrections, and team workflows uses
the application/backend. See [CPU readiness and metric limits](docs/setup/local-worker.md#profiles-and-model-readiness).

## Repository layout

```
Football-IQ/
├── backend/          # FastAPI app, DB job queue, storage facade, scheduler, Alembic, tests
├── frontend/         # Next.js static-export app, Vitest unit tests, Playwright E2E
├── gpu-worker/       # Pipeline: orchestrator, stages, turnkey CLI, DB-queue worker (CPU-capable)
├── docs/             # Architecture docs and ADRs
├── reports/          # Spike write-ups and evaluation reports
└── .env.example
```

## Environment variables

The backend reads its `.env`; the worker reads process environment. The native
launcher supplies local paths and credentials. `.env.example` documents shared
defaults and optional settings; hosted credentials remain blank. Backend typed
settings live in [`backend/app/config.py`](backend/app/config.py).

The connection points worth knowing:

| Variable | What it controls |
|---|---|
| `DATABASE_URL` / `DATABASE_SYNC_URL` | Postgres (app / Alembic). Any Postgres 16+ with pgvector. |
| `STORAGE_BACKEND` | `local` (disk, default) or `s3` (any S3-compatible endpoint). |
| `S3_*` | Object-store endpoint, credentials, and bucket names. Only read when `STORAGE_BACKEND=s3`. |
| `NEXT_PUBLIC_API_URL` | Backend origin, **baked into the frontend bundle at build time** (static export). |
| `CORS_ORIGINS` | Exact frontend origins the API accepts. A deployed origin missing here shows up as "login doesn't work". |
| `BACKEND_API_URL` | Where the pipeline worker claims jobs from. |
| `PIPELINE_DEVICE` / `PIPELINE_CPU_THREADS` | CPU by default, with bounded inference threads; local acceleration is explicit. |
| `PROCESSING_PROFILE` | `review`, `quick`, or `detailed`, independent of legacy queue priority. |
| `MODEL_DETECT_PATH` | Installed local player weights; missing models produce unavailable/partial analysis. |
| `PIPELINE_STORAGE_TRANSPORT` | `api` for authenticated backend media transfer; `direct` for shared disk/object-store access. |

## Deploying

The app can run entirely locally or connect local CPU processing to the existing
Cloudflare/Vercel deployment. Its service boundaries remain deployable:

- **Backend** — any container host. `backend/Dockerfile` builds it; it needs
  `DATABASE_URL`, `SECRET_KEY`, and `CORS_ORIGINS` set to the frontend's real
  origin. Run `alembic upgrade head` on deploy.
- **Frontend** — a static export (`npm run build` → `out/`), servable by any
  static host or CDN. `NEXT_PUBLIC_API_URL` must be set **at build time**.
- **Pipeline worker** — any host that can reach the backend; `gpu-worker/Dockerfile`
  builds a CPU image by default. Dedicated GPUs and hosted GPU providers are
  optional; no burst-provider integration is configured.
- **Database** — any managed or self-hosted Postgres 16+ with pgvector.
- **Object storage** — optional. Local disk is the default; set `S3_*` and
  `STORAGE_BACKEND=s3` to move objects to an S3-compatible bucket.

`.github/workflows/ci.yml` runs lint, typecheck, tests, and the migration
round-trip. `.github/workflows/deploy.yml` deploys the edge Worker (and the
backend container image) on pushes to `main` once the `CLOUDFLARE_API_TOKEN`
and `CLOUDFLARE_ACCOUNT_ID` repository secrets are set; the frontend deploys
via Vercel's git integration.

## Settings & Reports

System and per-user settings persist in the `system_settings` and `user_settings` Postgres tables. The frontend Settings page reads/writes them via `/api/v1/settings`. To reset local config:

```bash
psql "$DATABASE_SYNC_URL" -c 'DELETE FROM system_settings; DELETE FROM user_settings;'
```

Coaching reports are generated by `POST /api/v1/reports` (PDF/CSV/JSON), stored through the storage facade (`artifacts` bucket), and downloaded via signed URLs from `GET /api/v1/reports/{id}/download`. PDF rendering uses `reportlab` (pure-Python). See [`reports/templates/coaching_summary.md`](reports/templates/coaching_summary.md) for the template contract.

## Running tests

```bash
# Backend (needs a scratch Postgres with pgvector for some suites)
cd backend && pip install -r requirements-dev.txt && pytest -v

# Pipeline worker unit suite (stub mode, no torch needed)
cd gpu-worker && pip install -r requirements-ci.txt && pytest -v

# Cross-service integration (API → queue → worker → clips, real backend + Postgres)
cd gpu-worker && DATABASE_URL=... pytest -m integration tests/integration/ -v

# Frontend unit tests
cd frontend && npm test

# Frontend E2E (Playwright, fully offline/mocked)
cd frontend && npm run e2e:install && npm run e2e
```

## Contributing

See [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md). Open issues and PRs against the `main` branch.

Adding an external dataset, model, API, or library? Football-IQ is an **American
football** platform — soccer / association-football resources are rejected.
Follow the rubric, soccer denylist, and license gate in
[docs/external-resource-rubric.md](docs/external-resource-rubric.md) before proposing one.
