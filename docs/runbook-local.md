# Local Development Runbook

This runbook lets a new engineer or AI agent bring Football-IQ up end to end on a local machine with minimal guesswork. Work through the sections in order; each section references the actual files and commands in the repo.

**The platform runs fully locally with no cloud accounts and no GPU.** Cloudflare (Worker + R2 + edge downloads) is a production/cloud deployment concern; a GPU is a speed upgrade, not a requirement — the pipeline runs real YOLO on CPU for short clips and falls back to deterministic stubs where heavy model stacks are absent.

---

## Prerequisites

| Tool | Minimum version | Install |
|------|----------------|---------|
| Docker + Docker Compose | 24+ | https://docs.docker.com/get-docker/ |
| Node.js | 20+ | https://nodejs.org/ |
| Python | 3.12 | https://python.org/ |
| npm | 10+ | bundled with Node |
| ffmpeg | 5+ | only if running the pipeline outside Docker |
| (optional) wrangler CLI | 3+ | `npm install -g wrangler` — cloud-sim only |

An NVIDIA GPU (CUDA 12.4+, NVIDIA Container Toolkit) accelerates the pipeline but is **optional** — every stage has a CPU path.

---

## Repository architecture

```
Football-IQ/
├── backend/              FastAPI backend (Python 3.12)
│   ├── app/
│   │   ├── main.py       FastAPI entrypoint (+ nightly scheduler lifespan)
│   │   ├── config.py     Pydantic settings (reads from env)
│   │   ├── models.py     SQLAlchemy ORM models
│   │   ├── storage.py    Storage facade: local disk or R2, signed URLs
│   │   ├── scheduler.py  Nightly tick: corrections export → training job
│   │   └── routers/      videos, clips, jobs (claim/heartbeat), uploads,
│   │                     storage (signed local streaming), auth, corrections, …
│   ├── scripts/seed_users.py   Idempotent admin + worker service account
│   ├── migrations/       Alembic migration scripts
│   └── tests/            Pytest (some suites use a real Postgres — see below)
│
├── frontend/             Next.js 16 static-export app (React 19, TypeScript)
│   ├── src/app/          Routes: dashboard, film-room, clip-review, scouting, …
│   ├── src/lib/          api.ts client, auth.tsx (login/JWT), app-state
│   └── e2e/              Playwright specs (fully mocked, offline)
│
├── workers/              Cloudflare Worker (cloud deployments only)
│   └── src/              Upload proxy to R2, signed /dl/*, HLS. Job dispatch
│                         lives in the backend DB queue, not here.
│
├── gpu-worker/           Video pipeline (CPU-capable; GPU optional)
│   ├── __main__.py       Worker loop: claims jobs from the backend DB queue
│   ├── pipeline/
│   │   ├── orchestrator.py   Chains all stages in-process, resume ledger
│   │   ├── __main__.py       Turnkey CLI: python -m pipeline run --input …
│   │   ├── storage.py        r2:// | local:// | file:// facade
│   │   └── stage_*.py        detect, track, reid, pose, events, render, …
│   └── tests/            Unit suite + tests/integration (cross-service)
│
├── docs/                 Architecture docs and ADRs
├── docker-compose.yml    Profiles: default, migrate, pipeline, cloud-sim
└── .env.example          Environment variable template
```

**Data flow for a new upload (local mode, the default):**

1. Coach signs in (JWT) and uploads an MP4 in the frontend.
2. With `NEXT_PUBLIC_WORKER_URL` empty, the frontend calls the backend's Worker-parity endpoints: `POST /api/v1/videos/upload-url` → `PUT /api/v1/videos/upload/{key}`. The file lands under `LOCAL_STORAGE_ROOT` as `local://raw-video/raw/…`.
3. Registering the video (`POST /api/v1/videos`) **auto-enqueues a `pipeline` job** (system setting `auto_process_on_upload`, default ON; there is also an explicit "Process Film" button).
4. The **gpu-worker** claims the job from the backend DB queue (`POST /api/v1/jobs/claim`, `FOR UPDATE SKIP LOCKED` with leases), runs the whole stage chain in-process via the orchestrator, and heartbeats per-stage progress into the job row.
5. Results (clips, tracklets, events, metrics, overlay MP4s) are written back through the backend API with the worker's service account.
6. The frontend polls jobs/clips; clip review streams the overlay video through the backend's signed `GET /api/v1/storage/{bucket}/{key}` route (Range-supporting).

**Cloud mode** swaps step 2 for the Cloudflare Worker + R2 (`STORAGE_BACKEND=r2`) and step 6 for the Worker's signed `/dl/*` — same contracts, chosen by configuration. Job dispatch is the DB queue in both modes.

---

## Quick start (turnkey)

```bash
cp .env.example .env      # defaults work for local; edit only if you want to

# 1. Database + migrations + seed users
docker compose up -d db
docker compose --profile migrate up migrate seed

# 2. Everything: backend + frontend + pipeline worker
docker compose --profile pipeline up backend frontend gpu-worker
```

Open `http://localhost:3000`, sign in, upload a clip, watch it process.

**Signing in the first time — three options:**

- Seeded admin: `admin@example.com` / `change-me-admin` (override with `SEED_ADMIN_EMAIL` / `SEED_ADMIN_PASSWORD` before running the `seed` service).
- Register: the **first user ever registered becomes admin** automatically; everyone after that starts as `viewer` (an admin promotes them via `PATCH /api/v1/auth/users/{id}/role`). Client-supplied roles are ignored.
- Dev autologin: compose sets `DEV_AUTOLOGIN=1`, enabling `POST /api/v1/auth/dev-login` (development environment only) which the login page surfaces as a one-click dev sign-in.

`PIPELINE_STUB=1 docker compose --profile pipeline up …` swaps real models for deterministic stubs — useful to verify wiring in seconds on any machine.

---

## Turnkey CLI (mp4 in → boxes out, no services)

The orchestrator also runs standalone — point it at any file or directory (any camera, any angle; e.g. the `Drone Footage/` clips):

```bash
cd gpu-worker
python -m pipeline run --input "../Drone Footage/DJI_0119.mp4" --no-backend --out ./out
```

Outputs per video under `./out/<name>/`: `summary.json` (clips, tracklets, stage timings, model routing), per-stage artifact JSONs, and rendered overlay MP4s with bounding boxes, trails, and (when calibrated) metric callouts. `--stages`, `--stride`, and `--mode same_session|nightly` narrow or reshape the run; without `--no-backend` it writes results into a live backend instead.

---

## Environment variables

Copy the template and adjust as needed:

```bash
cp .env.example .env
```

### Core local loop

| Variable | Docker Compose default | What it is |
|----------|-----------------------|------------|
| `ENVIRONMENT` | `development` | App mode |
| `SECRET_KEY` | `dev-secret-key-not-for-production` | JWT + signed-URL key (any 32+ char string locally) |
| `DATABASE_URL` | `postgresql+asyncpg://footiq:footiq_dev@db:5432/footiq` | Async DB URL (FastAPI) |
| `DATABASE_SYNC_URL` | `postgresql://footiq:footiq_dev@db:5432/footiq` | Sync DB URL (Alembic) |
| `CORS_ORIGINS` | `http://localhost:3000` | Allowed origins |
| `NEXT_PUBLIC_API_URL` | `http://localhost:8000` | Backend URL (browser-visible) |
| `NEXT_PUBLIC_WORKER_URL` | `""` (empty) | Empty → uploads/downloads go through the backend; set to a Worker URL only in cloud mode |

### Storage & pipeline (local vs cloud)

| Variable | Default | What it is |
|----------|---------|-----------|
| `STORAGE_BACKEND` | auto (`local` unless R2 creds present) | `local` or `r2` — one switch for backend and gpu-worker |
| `LOCAL_STORAGE_ROOT` | `/data/storage` in compose | Root for `local://bucket/key` objects |
| `PUBLIC_API_BASE_URL` | `http://localhost:8000` | Base baked into signed local streaming URLs |
| `QUEUE_BACKEND` | `db` | Worker job source: `db` (backend queue) or `cf` (legacy Cloudflare pull) |
| `WORKER_EMAIL` / `WORKER_PASSWORD` | seed defaults | The gpu-worker's service-account login (analyst role) |
| `SEED_ADMIN_EMAIL` / `SEED_ADMIN_PASSWORD` | `admin@example.com` / `change-me-admin` | `scripts/seed_users.py` inputs |
| `SEED_WORKER_EMAIL` / `SEED_WORKER_PASSWORD` | `worker@example.com` / `change-me-worker` | Ditto, worker account |
| `DEV_AUTOLOGIN` | off | `1` + development env → enables `POST /auth/dev-login` |
| `PIPELINE_STUB` | `0` | `1` → deterministic stub models (fast wiring checks) |
| `CF_QUEUES_ENABLED` | off | Opt-in for legacy Cloudflare Queues publishing |
| `SCHEDULER_ENABLED` / `SCHEDULER_HOUR_UTC` | on / `8` | Nightly learning-loop tick (corrections export → training job when ≥ `TRAINING_MIN_NEW_LABELS`, default 200) |

### R2 / Cloudflare variables (cloud mode only)

Needed only when `STORAGE_BACKEND=r2` (real R2 or the MinIO cloud-sim) or when running the Worker:

| Variable | What it is |
|----------|-----------|
| `CLOUDFLARE_ACCOUNT_ID` | Your Cloudflare account ID |
| `CLOUDFLARE_API_TOKEN` | API token with Workers + R2 permissions |
| `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` | R2 (or MinIO) credentials |
| `R2_ENDPOINT_URL` | `https://<ACCOUNT_ID>.r2.cloudflarestorage.com` (or `http://minio:9000`) |
| `R2_BUCKET_RAW` / `R2_BUCKET_CLIPS` / `R2_BUCKET_OVERLAYS` / `R2_BUCKET_ARTIFACTS` | `raw-video` / `clips` / `overlays` / `artifacts` |
| `R2_PRESIGN_TTL` | Presigned URL lifetime seconds (default `3600`) |

The Worker reads `JWT_SECRET`, `DATABASE_URL`, and `BACKEND_API_URL` as **Wrangler secrets**:

```bash
cd workers
npx wrangler secret put JWT_SECRET
npx wrangler secret put DATABASE_URL
npx wrangler secret put BACKEND_API_URL
```

### JWT variables

| Variable | Default | What it is |
|----------|---------|-----------|
| `JWT_ALGORITHM` | `HS256` | `HS256` or `RS256` |
| `JWT_ACCESS_TOKEN_EXPIRE_MINUTES` | `60` | Access token lifetime |
| `JWT_REFRESH_TOKEN_EXPIRE_DAYS` | `30` | Refresh token lifetime |

### College Football Data (CFBD) — backend-only (optional)

| Variable | What it is |
|----------|-----------|
| `CFBD_API_KEY` | College Football Data API key — **backend only** |
| `CFBD_BASE_URL` | API base URL (default `https://api.collegefootballdata.com`) |

CFBD powers the Toledo/MAC analytics cache (Issues #160/#161/#162) and is called **only** from the FastAPI backend — never from the frontend, the Worker, or any browser bundle. The key is never persisted to the database and never appears in logs or coach-visible errors.

To set it up locally **without committing any value**:

1. Request a free key at <https://collegefootballdata.com/key>.
2. Add it to your local, git-ignored `.env` (copied from `.env.example`).
3. Leave `CFBD_BASE_URL` at its default unless you are pointing at a mock.

If `CFBD_API_KEY` is unset, the app still boots normally; any CFBD call fails fast with a clear backend-only `CFBDConfigError` rather than an opaque 401, and previously cached data in the `cfbd_*` tables remains fully queryable.

To populate the cache for a season once the key is set:

```bash
cd backend
python -m app.cfbd --season 2024                 # Toledo + MAC, regular season
python -m app.cfbd --season 2024 --season-type postseason
```

The command upserts idempotently (safe to re-run) and records every attempt in `cfbd_sync_runs`.

### Deployment variables (not needed locally)

`FLY_API_TOKEN` and `FLY_APP_NAME` are only used by the CD pipeline (`cd.yml`). Do not set them locally.

---

## Running services outside Docker

**Database** — any Postgres 16 with pgvector. Simplest:

```bash
docker run -d --name footiq-db \
  -e POSTGRES_USER=footiq -e POSTGRES_PASSWORD=footiq_dev -e POSTGRES_DB=footiq \
  -p 5432:5432 pgvector/pgvector:pg16
```

**Backend:**

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

export DATABASE_URL=postgresql+asyncpg://footiq:footiq_dev@localhost:5432/footiq
export DATABASE_SYNC_URL=postgresql://footiq:footiq_dev@localhost:5432/footiq
export SECRET_KEY=dev-secret-key-not-for-production
export CORS_ORIGINS=http://localhost:3000
export STORAGE_BACKEND=local LOCAL_STORAGE_ROOT=/tmp/footiq-storage
export PUBLIC_API_BASE_URL=http://localhost:8000

alembic upgrade head
SEED_ADMIN_EMAIL=admin@example.com SEED_ADMIN_PASSWORD=change-me-admin \
SEED_WORKER_EMAIL=worker@example.com SEED_WORKER_PASSWORD=change-me-worker \
  python -m scripts.seed_users
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

**Frontend:**

```bash
cd frontend
npm ci
export NEXT_PUBLIC_API_URL=http://localhost:8000
export NEXT_PUBLIC_WORKER_URL=""
npm run dev            # http://localhost:3000
```

**Pipeline worker** (needs ffmpeg on PATH; torch optional):

```bash
cd gpu-worker
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt        # or requirements-ci.txt for stub mode

export QUEUE_BACKEND=db BACKEND_API_URL=http://localhost:8000
export WORKER_EMAIL=worker@example.com WORKER_PASSWORD=change-me-worker
export STORAGE_BACKEND=local LOCAL_STORAGE_ROOT=/tmp/footiq-storage
python -m __main__ 2>/dev/null || python __main__.py
```

---

## Cloud-sim (verify the cloud path without a Cloudflare account)

MinIO speaks the same S3 API surface boto3 uses against R2, so this exercises the **exact** `r2` storage driver:

```bash
docker compose --profile cloud-sim up -d minio minio-init   # buckets auto-created

# Point backend + gpu-worker at it:
export STORAGE_BACKEND=r2
export R2_ENDPOINT_URL=http://localhost:9000     # http://minio:9000 inside compose
export R2_ACCESS_KEY_ID=minio-local R2_SECRET_ACCESS_KEY=minio-local-secret
```

For the edge upload contract, additionally run the Worker under wrangler's Miniflare (`cd workers && npx wrangler dev`, `.dev.vars` for secrets) and set `NEXT_PUBLIC_WORKER_URL=http://localhost:8787`. Real deployment to Cloudflare/Fly stays a credentialed handoff step.

---

## Database

### Applying migrations

```bash
docker compose --profile migrate up migrate     # or: cd backend && alembic upgrade head
```

### Creating a new migration

```bash
cd backend
alembic revision --autogenerate -m "describe_your_change"
# Review the generated file in migrations/versions/ before committing
alembic upgrade head
```

### Rolling back / inspecting

```bash
cd backend
alembic downgrade -1
alembic current
alembic history --verbose
```

### pgvector

The compose `db` service uses `pgvector/pgvector:pg16` (upstream Postgres 16 with pgvector preinstalled), which migration `0008_play_embeddings.py`'s `CREATE EXTENSION IF NOT EXISTS vector` requires. Managed Postgres (Supabase, Neon) bundles it too. A plain `postgres:16` image will fail that migration — use the pgvector image.

### Seed data

```bash
docker compose --profile migrate up seed        # or: cd backend && python -m scripts.seed_users
```

Idempotent; creates/updates the admin and the gpu-worker service account from the `SEED_*` variables. Beyond users, upload real footage — the pipeline generates the rest (clips, tracklets, metrics).

---

## Backend tests

```bash
cd backend
source .venv/bin/activate
pip install -r requirements-dev.txt

export SECRET_KEY=any-32-char-string
export ENVIRONMENT=test
export DATABASE_URL=postgresql+asyncpg://footiq:footiq_test@localhost:5432/footiq_test

pytest -v
```

Most suites mock the DB, but several (`test_jobs_claim.py` lease/SKIP LOCKED semantics, `test_storage_local.py`, `test_scheduler.py`, the CFBD cache suite) need `DATABASE_URL` to point at a **real, scratch Postgres with pgvector** — never the dev database. They create and drop their own tables; the CFBD suite expects to start from an empty schema (CI runs pytest before the alembic up/down check for this reason). To reset a polluted scratch DB:

```sql
DROP SCHEMA public CASCADE; CREATE SCHEMA public; CREATE EXTENSION vector;
```

### Linting and type checking

```bash
cd backend
ruff check . && ruff format --check . && mypy app
```

---

## GPU-worker tests

```bash
cd gpu-worker
pip install -r requirements-ci.txt   # stub mode: no torch needed
ruff check .
pytest -v                            # unit suite (integration excluded by default)
```

### Cross-service integration test

Boots the real backend against Postgres, uploads a synthetic clip through the API, runs one worker claim/process cycle in-process, and asserts job progress, clips, tracklets, and overlays through the API:

```bash
cd gpu-worker
DATABASE_URL=postgresql+asyncpg://footiq:footiq_test@localhost:5432/footiq_test \
  pytest -m integration tests/integration/ -v
```

Requirements: scratch Postgres with pgvector (same warning as backend tests), ffmpeg, and a `backend/.venv` (preferred; falls back to the current interpreter). CI runs this as the `integration` job.

---

## Frontend tests

### Unit tests (Vitest)

```bash
cd frontend
npm ci && npm test
```

### E2E tests (Playwright)

Fully offline: runs `next dev` and intercepts all backend HTTP calls (auth included — the helpers seed a fake JWT).

```bash
cd frontend
npm run e2e:install     # one-time Chromium download
npm run e2e
```

If a Chromium already exists on the machine, point at it instead of downloading: `PLAYWRIGHT_EXECUTABLE_PATH=/path/to/chromium npm run e2e`. The web server starts on port `3100` (`E2E_PORT` overrides).

### Lint and type check

```bash
cd frontend
npm run lint && npm run typecheck
```

---

## Troubleshooting

### API issues

**`curl http://localhost:8000/health` refused** — `docker compose ps`; `docker compose logs backend`; verify `DATABASE_URL`/`SECRET_KEY` (the app fails fast if `app.config.Settings` cannot parse them).

**401s from the frontend** — you're not signed in, or the token expired and refresh failed. Sign in again; check the browser console for the failing call. All API routes except `/health`, auth, and signed storage streaming require a Bearer token.

**403 on an action** — role too low. Roles: `viewer < coach < analyst < admin`. Corrections need coach+; user management needs admin.

### Jobs sit in `queued` forever

- Is the worker running? It's behind the `pipeline` profile: `docker compose --profile pipeline up gpu-worker`.
- Check its logs: `docker compose logs gpu-worker`. Login failures mean `WORKER_EMAIL`/`WORKER_PASSWORD` don't match a seeded account — rerun the `seed` service.
- Inspect the queue directly: `psql "$DATABASE_SYNC_URL" -c "SELECT id, job_type, status, leased_by, attempt_count, lease_expires_at FROM processing_jobs ORDER BY created_at DESC LIMIT 10;"`
- A crashed worker's lease expires on its own (default 600 s); the job is then re-claimable. After `max_attempts` (3) it lands in `failed` with the error preserved — the UI's retry clones it.

### Upload or playback fails (local mode)

- `STORAGE_BACKEND` must be `local` on **both** backend and gpu-worker, with the **same** volume mounted at `LOCAL_STORAGE_ROOT` (compose shares the `storage_data` volume).
- Playback URLs are HMAC-signed with `SECRET_KEY` and expire; a 403 from `/api/v1/storage/...` usually means the page sat open past expiry — reload — or the backend's `SECRET_KEY` changed.
- `PUBLIC_API_BASE_URL` must be the browser-reachable backend URL, or signed links will point somewhere the browser can't reach.

### CORS issues

- Check `CORS_ORIGINS` matches the frontend origin (comma-separated list, e.g. `http://localhost:3000,http://localhost:3001`).
- Worker CORS (cloud mode) is configured independently in `workers/src/index.ts`.

### Database / migration issues

- `relation already exists` → partial migration ran; check `alembic current`. Clean slate: `docker compose down -v` (drops volumes) and re-migrate.
- `FATAL: role "footiq" does not exist` → backend connected before Postgres finished init; the compose healthcheck handles this, manual starts must wait for `pg_isready`.
- `CREATE EXTENSION vector` fails → not a pgvector image; see [pgvector](#pgvector).

### Pipeline issues

- **Zero clips on a tiny/synthetic test video** is usually the optical-flow segmenter refusing sub-3 s segments — expected. Real footage segments fine; for wiring checks use `PIPELINE_STUB=1`.
- **Spatial metrics missing** on some footage is by design: when field lines can't be detected from the camera angle, calibration marks the video `analytics_safe=false` and the UI explains why. Boxes, clips, and tracking still work.
- **Model weights**: Ultralytics downloads YOLO weights on first run (cached under `~/.config/Ultralytics`); ensure outbound internet once, or pre-place the `.pt` files in `gpu-worker/`. A promoted model from the registry is fetched automatically into `~/.cache/football-iq/models/`.
- **No GPU** → everything still runs; expect ~1–2 min per 30 s clip on a laptop CPU for the same-session stage set. `nvidia-smi` + NVENC are picked up automatically when present (with a runtime probe, not just a listing).

### Worker / queue issues (cloud legacy)

`QUEUE_BACKEND=cf` keeps the old Cloudflare Queues pull path for deployments that still use it; it requires the `CF_*`/`CLOUDFLARE_*` variables and a deployed Worker. New setups should stay on `db`.

---

## Service start order

```
1. db          → docker compose up -d db
2. migrate     → docker compose --profile migrate up migrate
3. seed        → docker compose --profile migrate up seed
4. backend     → docker compose up -d backend
5. frontend    → docker compose up -d frontend
6. gpu-worker  → docker compose --profile pipeline up -d gpu-worker
7. (cloud-sim) → docker compose --profile cloud-sim up -d; workers: npx wrangler dev
```

Steps 1–5 give the full UI and API. Step 6 adds video processing. Step 7 only exists to rehearse the cloud storage/edge path.
