# Cloudflare setup

Manual steps to stand up the API. Everything here is one-time; after this,
`wrangler deploy` is the whole deployment.

The shape: a TypeScript Worker at the edge (`workers/api-edge`) handles R2
multipart uploads, signed object streaming, and the nightly cron, and proxies
everything else to the FastAPI backend running as a **Cloudflare Container**.

```
Vercel (Next.js)
   │  HTTPS
   ▼
Cloudflare Worker ──── R2 (raw-video, clips, overlays, artifacts)
                  ──── Hyperdrive ──► Postgres   (Worker-side queries only)
   │ proxy /api/v1/*
   ▼
Cloudflare Container (FastAPI) ─────► Postgres   (direct — see §4)
   ▲ claim / heartbeat / writeback
Lambda Labs GPU worker
```

## Prerequisites

- A Cloudflare account on the **Workers Paid** plan. Containers are not
  available on the free plan.
- Docker running locally. `wrangler deploy` builds `backend/Dockerfile` on your
  machine and pushes the image.
- A Postgres 16 database with `pgvector`, **reachable from the public internet**
  (see §4 for why).
- `npm install` inside `workers/api-edge`.

Log in once:

```bash
cd workers/api-edge
npx wrangler login
```

---

## 1. R2 buckets

These already exist — **do not recreate them**, that would orphan whatever is
already stored:

```
footiq-raw-video   footiq-clips   footiq-overlays   footiq-artifacts
```

Confirm they are all present:

```bash
npx wrangler r2 bucket list
```

No CORS configuration is needed. The browser never talks to R2 directly: uploads
go through the Worker's multipart endpoints and playback through its signed
streaming route, both same-origin.

### R2 API token

The **container** writes clips, overlays and reports through the S3 API, so it
needs credentials of its own — the Worker's R2 *bindings* do not reach inside
it.

Dashboard → **R2** → **Manage R2 API Tokens** → *Create API token*:

- Permission: **Object Read & Write**
- Scope it to the four buckets above rather than the whole account.

Keep the Access Key ID, Secret Access Key, and the
`https://<ACCOUNT_ID>.r2.cloudflarestorage.com` endpoint. They become secrets in
§5.

---

## 2. Postgres

Any Postgres 16+ with `pgvector`. Provisioning PlanetScale Postgres from the
Cloudflare dashboard bills it on your Cloudflare invoice; Neon works the same
way from outside.

Enable the extension and run the schema:

```bash
psql "$DATABASE_SYNC_URL" -c 'CREATE EXTENSION IF NOT EXISTS vector;'
cd backend && alembic upgrade head
```

`alembic upgrade head` runs from your machine, not from the container. There is
no automatic migration step on deploy, deliberately: a schema change should be a
decision, not a side effect of shipping code.

---

## 3. Hyperdrive

Create it with caching **off**:

```bash
npx wrangler hyperdrive create footiq-db \
  --connection-string="postgres://USER:PASSWORD@HOST:5432/footiq" \
  --caching-disabled
```

Put the returned id into `hyperdrive[0].id` in `wrangler.jsonc`, replacing
`REPLACE_WITH_HYPERDRIVE_CONFIG_ID`.

**Caching has to stay disabled.** Hyperdrive does not invalidate cached reads
when you write, and this app's hot paths are all read-after-write:

| Path | What a stale read does |
|---|---|
| Register → login | The account exists, and the login says it does not |
| `POST /jobs/claim` | A worker claims a job a cached read still shows as queued |
| Upload → register → inbox | The film uploads and never appears |

If you later add a genuinely read-only Worker surface that would benefit from
caching, add a *second* Hyperdrive config with caching on and bind it separately,
rather than turning it on for this one.

---

## 4. Why the container does not use Hyperdrive

A Hyperdrive connection string only resolves **inside the Workers runtime**. The
container is a separate sandbox; handing it that string produces connection
failures that look like a database outage.

So the container dials Postgres directly, via the `BACKEND_DATABASE_URL` secret.
That is why the database has to be publicly reachable.

This costs nothing. Hyperdrive's win is eliminating the seven round trips of
per-request connection setup, which matters for a Worker that opens a connection
per request. A long-lived container already holds a SQLAlchemy connection pool.

Restrict access at the database instead: allowlist
[Cloudflare's IP ranges](https://www.cloudflare.com/ips/) in your provider's
firewall, and require TLS.

---

## 5. Secrets

**None of the Worker's secrets reach the container automatically.** They are
forwarded explicitly by `buildContainerEnv` in `src/container.ts`, which is
pinned by tests — a missing value here does not crash, it silently degrades
(local disk instead of R2, presigned URLs instead of Worker-served ones).

```bash
cd workers/api-edge

# Identical to the backend's. Signs JWTs and stream URLs; if the Worker and the
# backend disagree, every video URL 403s and every upload 401s.
npx wrangler secret put SECRET_KEY

# Authenticates the cron tick. Until this is set, /internal/scheduler/tick 404s.
npx wrangler secret put SCHEDULER_TOKEN

# postgresql+asyncpg://USER:PASSWORD@HOST:5432/footiq
npx wrangler secret put BACKEND_DATABASE_URL

# From the R2 API token in §1.
npx wrangler secret put R2_ACCESS_KEY_ID
npx wrangler secret put R2_SECRET_ACCESS_KEY
npx wrangler secret put R2_ENDPOINT_URL
```

Generate `SECRET_KEY` and `SCHEDULER_TOKEN` with real entropy:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

### Optional bootstrap admin

Only if you want the first admin to exist without registering through the UI.
Otherwise skip it — the **first account to register becomes admin automatically**,
which is the simpler path.

```bash
npx wrangler secret put SEED_ADMIN_EMAIL
npx wrangler secret put SEED_ADMIN_PASSWORD
```

Then set `SEED_USERS_ON_STARTUP` to `"true"` in `wrangler.jsonc` `vars`.

---

## 6. Vars

Edit `vars` in `wrangler.jsonc` before the first deploy:

| Var | Set it to |
|---|---|
| `CORS_ORIGINS` | Your real Vercel origin, e.g. `https://footiq.vercel.app`. Leaving it at localhost is the classic "login doesn't work in production" — the browser's preflight is rejected before the request reaches any endpoint. |
| `CORS_ORIGIN_REGEX` | Optional, for preview deploys: `https://footiq-[a-z0-9-]+\\.vercel\\.app`. It is anchored at both ends when compiled, so a lookalike origin cannot match. |
| `PUBLIC_API_BASE_URL` | This Worker's public origin. The backend mints absolute upload and playback URLs from it. |

---

## 7. Deploy

```bash
cd workers/api-edge
npx wrangler deploy
```

The first deploy builds and pushes the container image, so it takes a few
minutes. **Containers take several more minutes to provision after the deploy
reports success** — until then the Worker answers but calls into the container
error.

Check on it:

```bash
npx wrangler containers list
npx wrangler tail            # live logs
```

Verify the edge is up without waking the container:

```bash
curl https://<your-worker>.workers.dev/edge/health
# {"status":"ok","environment":"production"}
```

Then verify the container path end to end:

```bash
curl https://<your-worker>.workers.dev/health
```

---

## 8. Custom domain

Dashboard → **Workers & Pages** → your Worker → **Settings** → **Domains &
Routes** → *Add custom domain*. Then update `PUBLIC_API_BASE_URL`, the
frontend's `NEXT_PUBLIC_API_URL` (see [vercel.md](vercel.md)), and the GPU
worker's `BACKEND_API_URL`.

---

## 9. Things that will bite you

**Do not raise `max_instances` above 1, and do not raise uvicorn's worker count
in `backend/Dockerfile`.** `alerts_sse.py` keeps its subscriber map in process
memory, so a second process — another instance *or* a forked worker — silently
drops alert streams: the connection stays open and simply never emits. Both
limits have to lift together, and only once that map moves to a Durable Object
or the frontend falls back to polling.

**The cron is the only scheduler.** `SCHEDULER_ENABLED=0` is forwarded to the
container because a container that sleeps after 20 minutes is not running at
08:00 UTC to notice the tick is due. If you disable the cron trigger, the
correction → training flywheel stops turning, silently.

**Container cold starts are real.** The first request after 20 minutes idle
waits for a boot. That is the tradeoff for not paying for an idle API all week;
lengthen `sleepAfter` in `src/container.ts` if coaches notice it.
