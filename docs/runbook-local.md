# Local Development Runbook

This runbook brings Football-IQ up end to end on a local machine. Work through the sections in order; each references the actual files and commands in the repo.

**The platform runs fully locally with no cloud accounts and no GPU.** A GPU is a speed upgrade, not a requirement — the pipeline runs real YOLO on CPU for short clips and falls back to deterministic stubs where heavy model stacks are absent.

There is no deployment configuration in this repo. Hosting, object storage, and CD are deliberately unwired; see [Deploying](../README.md#deploying) for what each service needs when you pick providers.

---

## Prerequisites

| Tool | Minimum version | Install |
|------|----------------|---------|
| Python | 3.12 | https://python.org/ |
| Node.js | 20+ | https://nodejs.org/ |
| npm | 10+ | bundled with Node |
| PostgreSQL | 16, with the `pgvector` extension | https://www.postgresql.org/download/ |
| ffmpeg | 5+ | required by the pipeline (ingest probing + rendering) |

An NVIDIA GPU (CUDA 12.4+) accelerates the pipeline but is **optional** — every stage has a CPU path.

Docker is optional. `backend/Dockerfile`, `frontend/Dockerfile`, and `gpu-worker/Dockerfile` build each service if you prefer containers, but nothing in the repo orchestrates them for you.

---

## Repository architecture

```
Football-IQ/
├── backend/              FastAPI backend (Python 3.12)
│   ├── app/
│   │   ├── main.py       FastAPI entrypoint (+ nightly scheduler lifespan)
│   │   ├── config.py     Pydantic settings (reads from env)
│   │   ├── models.py     SQLAlchemy ORM models
│   │   ├── storage.py    Storage facade: local disk or S3-compatible, signed URLs
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
├── gpu-worker/           Video pipeline (CPU-capable; GPU optional)
│   ├── __main__.py       Worker loop: claims jobs from the backend DB queue
│   ├── queue/            Job priority buckets + dispatch (creates job rows)
│   ├── pipeline/
│   │   ├── orchestrator.py   Chains all stages in-process, resume ledger
│   │   ├── __main__.py       Turnkey CLI: python -m pipeline run --input …
│   │   ├── storage.py        s3:// | local:// | file:// facade
│   │   └── stage_*.py        detect, track, reid, pose, events, render, …
│   └── tests/            Unit suite + tests/integration (cross-service)
│
├── docs/                 Architecture docs and ADRs
└── .env.example          Environment variable template
```

**Data flow for a new upload:**

1. Coach signs in (JWT) and uploads an MP4 in the frontend.
2. The frontend calls `POST /api/v1/videos/upload-url` → `PUT /api/v1/videos/upload/{key}`. In the default local mode the file lands under `LOCAL_STORAGE_ROOT` as `local://raw-video/raw/…`; with `STORAGE_BACKEND=s3` it lands in the configured bucket as `s3://raw-video/raw/…`.
3. Registering the video (`POST /api/v1/videos`) **auto-enqueues a `pipeline` job** (system setting `auto_process_on_upload`, default ON; there is also an explicit "Process Film" button).
4. The **gpu-worker** claims the job from the backend DB queue (`POST /api/v1/jobs/claim`, `FOR UPDATE SKIP LOCKED` with leases), runs the whole stage chain in-process via the orchestrator, and heartbeats per-stage progress into the job row.
5. Results (clips, tracklets, events, metrics, overlay MP4s) are written back through the backend API with the worker's service account.
6. The frontend polls jobs/clips; clip review streams the overlay video through the backend's signed `GET /api/v1/storage/{bucket}/{key}` route (Range-supporting).

The job queue is the database in every configuration — there is no message broker to stand up.

---

## Quick start

```bash
cp .env.example .env      # defaults work for local; edit only if you want to
```

**1. Database**

```bash
createdb footiq
psql footiq -c 'CREATE EXTENSION IF NOT EXISTS vector;'
```

**2. Backend**

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

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

**3. Frontend**

```bash
cd frontend
npm ci
export NEXT_PUBLIC_API_URL=http://localhost:8000
npm run dev            # http://localhost:3000
```

**4. Pipeline worker** (needs ffmpeg on PATH; torch optional)

```bash
cd gpu-worker
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt        # or requirements-ci.txt for stub mode

export BACKEND_API_URL=http://localhost:8000
export WORKER_EMAIL=worker@example.com WORKER_PASSWORD=change-me-worker
export STORAGE_BACKEND=local LOCAL_STORAGE_ROOT=/tmp/footiq-storage
python __main__.py
```

`LOCAL_STORAGE_ROOT` must resolve to the **same directory** for the backend and the worker — they exchange files through it.

Open `http://localhost:3000`, sign in, upload a clip, watch it process.

**Signing in the first time — three options:**

- Seeded admin: `admin@example.com` / `change-me-admin` (override with `SEED_ADMIN_EMAIL` / `SEED_ADMIN_PASSWORD` before running `seed_users`).
- Register: the **first user ever registered becomes admin** automatically; everyone after that starts as `viewer` (an admin promotes them via `PATCH /api/v1/auth/users/{id}/role`). Client-supplied roles are ignored.
- Dev autologin: set `DEV_AUTOLOGIN=1` with `ENVIRONMENT=development` to enable `POST /api/v1/auth/dev-login`, which the login page surfaces as a one-click dev sign-in. Never enable it anywhere else.

`PIPELINE_STUB=1` on the worker swaps real models for deterministic stubs — useful to verify wiring in seconds on any machine.

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

`.env.example` is the full annotated list. The ones that matter for a local loop:

### Core

| Variable | Local default | What it is |
|----------|--------------|------------|
| `ENVIRONMENT` | `development` | App mode |
| `SECRET_KEY` | — | JWT + signed-URL key (any 32+ char string locally) |
| `DATABASE_URL` | `postgresql+asyncpg://footiq:footiq_dev@localhost:5432/footiq` | Async DB URL (FastAPI) |
| `DATABASE_SYNC_URL` | `postgresql://footiq:footiq_dev@localhost:5432/footiq` | Sync DB URL (Alembic) |
| `CORS_ORIGINS` | `http://localhost:3000` | Allowed origins (exact match, comma-separated) |
| `CORS_ORIGIN_REGEX` | empty | Optional pattern for per-deploy preview origins |
| `NEXT_PUBLIC_API_URL` | `http://localhost:8000` | Backend URL, **inlined into the frontend bundle at build time** |

### Storage & pipeline

| Variable | Default | What it is |
|----------|---------|-----------|
| `STORAGE_BACKEND` | auto (`local` unless `S3_*` creds present) | `local` or `s3` — one switch for backend and gpu-worker |
| `LOCAL_STORAGE_ROOT` | `./data/storage` | Root for `local://bucket/key` objects |
| `PUBLIC_API_BASE_URL` | `http://localhost:8000` | Base baked into signed local streaming URLs |
| `BACKEND_API_URL` | — | Where the worker claims jobs from |
| `WORKER_EMAIL` / `WORKER_PASSWORD` | seed defaults | The gpu-worker's service-account login (analyst role) |
| `SEED_ADMIN_EMAIL` / `SEED_ADMIN_PASSWORD` | `admin@example.com` / `change-me-admin` | `scripts/seed_users.py` inputs |
| `SEED_WORKER_EMAIL` / `SEED_WORKER_PASSWORD` | `worker@example.com` / `change-me-worker` | Ditto, worker account |
| `DEV_AUTOLOGIN` | off | `1` + development env → enables `POST /auth/dev-login` |
| `PIPELINE_STUB` | `0` | `1` → deterministic stub models (fast wiring checks) |
| `SCHEDULER_ENABLED` / `SCHEDULER_HOUR_UTC` | on / `8` | Nightly learning-loop tick (corrections export → training job when ≥ `TRAINING_MIN_NEW_LABELS`, default 200) |

### Object storage (only when `STORAGE_BACKEND=s3`)

Any S3-compatible endpoint works — AWS S3, MinIO, Backblaze B2, Cloudflare R2, and so on. Objects are addressed as `s3://bucket/key` and served through presigned URLs.

| Variable | What it is |
|----------|-----------|
| `S3_ACCESS_KEY_ID` / `S3_SECRET_ACCESS_KEY` | Object-store credentials |
| `S3_ENDPOINT_URL` | Your provider's S3 API endpoint |
| `S3_BUCKET_RAW` / `S3_BUCKET_CLIPS` / `S3_BUCKET_OVERLAYS` / `S3_BUCKET_ARTIFACTS` | `raw-video` / `clips` / `overlays` / `artifacts` |
| `S3_PRESIGN_TTL` | Presigned URL lifetime in seconds (default `3600`) |

To rehearse the object-store path without a cloud account, run MinIO locally and point `S3_ENDPOINT_URL` at it — boto3 talks to it through the exact same driver:

```bash
docker run -d --name footiq-minio -p 9000:9000 -p 9001:9001 \
  -e MINIO_ROOT_USER=minio-local -e MINIO_ROOT_PASSWORD=minio-local-secret \
  minio/minio server /data --console-address ":9001"
# Create the four buckets via the console at http://localhost:9001, then:
export STORAGE_BACKEND=s3
export S3_ENDPOINT_URL=http://localhost:9000
export S3_ACCESS_KEY_ID=minio-local S3_SECRET_ACCESS_KEY=minio-local-secret
```

### JWT

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

CFBD powers the Toledo/MAC analytics cache (Issues #160/#161/#162) and is called **only** from the FastAPI backend — never from the frontend or any browser bundle. The key is never persisted to the database and never appears in logs or coach-visible errors.

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

---

## Database

### Applying migrations

```bash
cd backend && alembic upgrade head
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

Migration `0008_play_embeddings.py` runs `CREATE EXTENSION IF NOT EXISTS vector`, so the server needs pgvector installed. On Debian/Ubuntu that is `postgresql-16-pgvector`; the `pgvector/pgvector:pg16` Docker image bundles it; managed Postgres (Supabase, Neon, RDS) offers it as an extension. A plain `postgres:16` with no pgvector package will fail that migration.

### Seed data

```bash
cd backend && python -m scripts.seed_users
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

`ffmpeg` must be on `PATH` — the orchestrator suite probes real files.

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

**`curl http://localhost:8000/health` refused** — check the uvicorn log; verify `DATABASE_URL`/`SECRET_KEY` (the app fails fast if `app.config.Settings` cannot parse them).

**401s from the frontend** — you're not signed in, or the token expired and refresh failed. Sign in again; check the browser console for the failing call. All API routes except `/health`, auth, and signed storage streaming require a Bearer token.

**403 on an action** — role too low. Roles: `viewer < coach < analyst < admin`. Corrections need coach+; user management needs admin.

### Jobs sit in `queued` forever

- Is the worker running, and is `BACKEND_API_URL` pointing at your backend?
- Check its log. Login failures mean `WORKER_EMAIL`/`WORKER_PASSWORD` don't match a seeded account — rerun `python -m scripts.seed_users`.
- Inspect the queue directly: `psql "$DATABASE_SYNC_URL" -c "SELECT id, job_type, status, leased_by, attempt_count, lease_expires_at FROM processing_jobs ORDER BY created_at DESC LIMIT 10;"`
- A crashed worker's lease expires on its own (default 600 s); the job is then re-claimable. After `max_attempts` (3) it lands in `failed` with the error preserved — the UI's retry clones it.

### Upload or playback fails (local mode)

- `STORAGE_BACKEND` must be `local` on **both** backend and gpu-worker, with `LOCAL_STORAGE_ROOT` resolving to the same directory for both.
- Playback URLs are HMAC-signed with `SECRET_KEY` and expire; a 403 from `/api/v1/storage/...` usually means the page sat open past expiry — reload — or the backend's `SECRET_KEY` changed.
- `PUBLIC_API_BASE_URL` must be the browser-reachable backend URL, or signed links will point somewhere the browser can't reach.

### CORS issues

- `CORS_ORIGINS` must contain the frontend's exact origin (comma-separated list, e.g. `http://localhost:3000,http://localhost:3001`). A missing origin fails the preflight before the request reaches any endpoint, which looks like "login doesn't work".
- Outside `ENVIRONMENT=development`, the backend logs a `cors_origins_localhost_only` warning at startup if you never set it.

### Database / migration issues

- `relation already exists` → partial migration ran; check `alembic current`. For a clean slate on a scratch DB: `DROP SCHEMA public CASCADE; CREATE SCHEMA public; CREATE EXTENSION vector;`
- `FATAL: role "footiq" does not exist` → create the role, or point `DATABASE_URL` at credentials that exist.
- `CREATE EXTENSION vector` fails → pgvector isn't installed on the server; see [pgvector](#pgvector).

### Pipeline issues

- **Zero clips on a tiny/synthetic test video** is usually the optical-flow segmenter refusing sub-3 s segments — expected. Real footage segments fine; for wiring checks use `PIPELINE_STUB=1`.
- **`FileNotFoundError: 'ffprobe'`** → ffmpeg isn't installed or isn't on `PATH`. The ingest stage and several orchestrator tests need it.
- **Spatial metrics missing** on some footage is by design: when field lines can't be detected from the camera angle, calibration marks the video `analytics_safe=false` and the UI explains why. Boxes, clips, and tracking still work.
- **Model weights**: Ultralytics downloads YOLO weights on first run (cached under `~/.config/Ultralytics`); ensure outbound internet once, or pre-place the `.pt` files in `gpu-worker/`. A promoted model from the registry is fetched automatically into `~/.cache/football-iq/models/`.
- **No GPU** → everything still runs; expect ~1–2 min per 30 s clip on a laptop CPU for the same-session stage set. `nvidia-smi` + NVENC are picked up automatically when present (with a runtime probe, not just a listing).

---

## Service start order

```
1. Postgres    → createdb footiq && psql footiq -c 'CREATE EXTENSION vector;'
2. Migrations  → cd backend && alembic upgrade head
3. Seed users  → cd backend && python -m scripts.seed_users
4. Backend     → uvicorn app.main:app --port 8000 --reload
5. Frontend    → cd frontend && npm run dev
6. GPU worker  → cd gpu-worker && python __main__.py
```

Steps 1–5 give the full UI and API. Step 6 adds video processing.
