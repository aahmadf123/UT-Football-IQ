# Football-IQ

Football-IQ is the Toledo Rockets' video intelligence platform. Coaches upload practice and game film — **from any camera, any angle, any height** — and the system tracks players, detects formations and coverages, generates analytics, and surfaces clips for review, all without manual tagging. Occasional one-click corrections feed a nightly learning loop that improves the models over time.

It runs **fully locally with no cloud accounts and no GPU** (CPU works; a GPU is a speed upgrade).

> **Deployment is intentionally unconfigured.** This repo ships application code
> and no hosting wiring: no deploy manifests, no provider credentials, no
> baked-in hostnames. Every connection point — database, object storage,
> frontend origin, CORS — is an environment variable with a local default. Pick
> your own providers and fill them in. See [Deploying](#deploying).

## Service map

| Service | Role | Tech |
|---------|------|------|
| **Frontend** | Coach-facing web UI: dashboard, film room, clip review with overlays, scouting, analytics | Next.js 16 (static export), React 19, TypeScript |
| **Backend API** | REST API: auth (JWT), videos, clips, the job queue (claim/lease/heartbeat), corrections, storage facade + signed streaming, nightly scheduler | FastAPI, Python 3.12, SQLAlchemy |
| **Database** | Primary data store, pgvector similarity index, and the **job queue** (`processing_jobs` with `FOR UPDATE SKIP LOCKED` leases) | PostgreSQL 16 + pgvector |
| **Pipeline worker** | Claims jobs, runs the full stage chain in-process (detect → track → re-ID → pose → events → metrics → render), writes results back through the API | Python; YOLOv8 + SAHI, RTMPose; CPU-capable, CUDA optional |
| **Storage** | `local://` disk (default) or `s3://` buckets `raw-video` / `clips` / `overlays` / `artifacts` — one `STORAGE_BACKEND` switch | Local volume or any S3-compatible object store |

There is no message broker and no edge service. The backend's `processing_jobs`
table *is* the queue, and the backend's nightly scheduler owns recurring work.

## Quick start (local)

Prerequisites: Python 3.12, Node 20+, PostgreSQL 16 with the `pgvector`
extension, and `ffmpeg` on `PATH`.

```bash
cp .env.example .env      # defaults target a local Postgres + local disk
```

**1. Database**

```bash
createdb footiq
psql footiq -c 'CREATE EXTENSION IF NOT EXISTS vector;'
```

**2. Backend**

```bash
cd backend
pip install -r requirements.txt
alembic upgrade head
python -m scripts.seed_users        # admin + gpu-worker service account
uvicorn app.main:app --reload --port 8000
```

**3. Pipeline worker** (separate shell)

```bash
cd gpu-worker
pip install -r requirements.txt
BACKEND_API_URL=http://localhost:8000 python __main__.py
```

**4. Frontend** (separate shell)

```bash
cd frontend
npm ci
npm run dev
```

Open `http://localhost:3000`, sign in (seeded `admin@example.com` /
`change-me-admin`, or register — the first user becomes admin), upload a clip,
and watch it process: per-stage progress on the dashboard, then clips with
bounding-box overlays in Clip Review. Uploads auto-process by default
(Settings → "Process film automatically on upload" turns it off).

**No services at all** — run the pipeline directly on any video file:

```bash
cd gpu-worker
python -m pipeline run --input "../Drone Footage/DJI_0119.mp4" --no-backend --out ./out
```

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

Copy `.env.example` to `.env`. It is the single source of truth for every knob:
the local defaults work as-is, and each cloud-facing value is blank so nothing
silently points at infrastructure you do not own. The backend's typed settings
live in [`backend/app/config.py`](backend/app/config.py).

The connection points worth knowing:

| Variable | What it controls |
|---|---|
| `DATABASE_URL` / `DATABASE_SYNC_URL` | Postgres (app / Alembic). Any Postgres 16+ with pgvector. |
| `STORAGE_BACKEND` | `local` (disk, default) or `s3` (any S3-compatible endpoint). |
| `S3_*` | Object-store endpoint, credentials, and bucket names. Only read when `STORAGE_BACKEND=s3`. |
| `NEXT_PUBLIC_API_URL` | Backend origin, **baked into the frontend bundle at build time** (static export). |
| `CORS_ORIGINS` | Exact frontend origins the API accepts. A deployed origin missing here shows up as "login doesn't work". |
| `BACKEND_API_URL` | Where the pipeline worker claims jobs from. |

## Deploying

Nothing here is prescribed — the app is three ordinary processes plus a
database, and each is a normal deployment target:

- **Backend** — any container host. `backend/Dockerfile` builds it; it needs
  `DATABASE_URL`, `SECRET_KEY`, and `CORS_ORIGINS` set to the frontend's real
  origin. Run `alembic upgrade head` on deploy.
- **Frontend** — a static export (`npm run build` → `out/`), servable by any
  static host or CDN. `NEXT_PUBLIC_API_URL` must be set **at build time**.
- **Pipeline worker** — any host that can reach the backend; `gpu-worker/Dockerfile`
  builds it. CUDA is optional.
- **Database** — any managed or self-hosted Postgres 16+ with pgvector.
- **Object storage** — optional. Local disk is the default; set `S3_*` and
  `STORAGE_BACKEND=s3` to move objects to an S3-compatible bucket.

`.github/workflows/ci.yml` runs lint, typecheck, tests, and the migration
round-trip. There is no deploy workflow — add one that matches whatever hosting
you choose.

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
