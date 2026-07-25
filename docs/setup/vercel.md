# Vercel setup

The frontend only. The API lives on Cloudflare — see
[cloudflare.md](cloudflare.md), and do that first: the frontend needs the
Worker's URL at **build** time.

## 1. Create the project

Vercel dashboard → **Add New** → **Project** → import `aahmadf123/UT-Football-IQ`.

Then, in the import screen:

| Setting | Value |
|---|---|
| Framework Preset | Next.js |
| **Root Directory** | **`frontend`** |
| Build Command | *(default)* |
| Output Directory | *(default)* |

The root directory is the one that is easy to miss. This is a monorepo; left at
the repository root, the build fails looking for a `package.json` that has no
`next` in it.

## 2. Environment variables

Add before the first deploy, for **Production**, **Preview**, and
**Development**:

| Name | Value |
|---|---|
| `NEXT_PUBLIC_API_URL` | Your Worker's origin, e.g. `https://api.footiq.example.com` — no trailing slash |

`NEXT_PUBLIC_*` variables are **inlined into the JavaScript bundle at build
time**, not read at runtime. Changing this value requires a redeploy; editing it
in the dashboard alone does nothing to the already-built bundle.

It is also public by definition. Never put an API key in a `NEXT_PUBLIC_*`
variable — the CFBD key and the R2 credentials are backend-only.

### Optional

| Name | Value | Purpose |
|---|---|---|
| `NEXT_PUBLIC_DEMO_ROLE` | `coach` / `analyst` / `sportsperformance` / `admin` | Renders the UI as that role when nobody is signed in. For demos and screenshots. Display only — the backend re-checks every request, so this grants nothing. |
| `NEXT_PUBLIC_USE_MOCKS` | `1` | Renders seeded sample data instead of calling the API. Leave **unset** in production. |

## 3. CORS

The Worker rejects any browser origin it does not recognise, and the rejection
happens at the preflight — before the request reaches an endpoint — so it
presents as "login doesn't work" rather than as a CORS error anyone notices.

Set `CORS_ORIGINS` in `workers/api-edge/wrangler.jsonc` to your production
Vercel domain and redeploy the Worker.

For preview deployments, whose URL changes per commit, set `CORS_ORIGIN_REGEX`
instead of listing them:

```jsonc
"CORS_ORIGIN_REGEX": "https://ut-football-iq-[a-z0-9-]+\\.vercel\\.app"
```

It is anchored at both ends when compiled, so `https://evil.com/?x=...vercel.app`
cannot satisfy it.

## 4. Custom domain

Project → **Settings** → **Domains**. After adding it, update `CORS_ORIGINS` on
the Worker and redeploy — the frontend moving origins is exactly the case that
silently breaks auth.

## 5. Verify

1. Open the deployment. You should land on `/login`.
2. Register the first account — **it becomes admin automatically**.
3. Sign in; the dashboard loads with empty states, not errors.
4. Open the browser console. No CORS errors on any `/api/v1/*` call.
5. Upload a clip from Film Room → Upload and confirm it appears in the Practice
   Inbox.

If step 3 shows "API offline" badges, the API URL or CORS is wrong. Check in
this order: `NEXT_PUBLIC_API_URL` is set *and the project has been redeployed
since*, then `CORS_ORIGINS` on the Worker, then `curl <worker>/edge/health`.
