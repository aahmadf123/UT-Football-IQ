---
name: football-iq-repo-guardrails
description: Repo guardrails for Football-IQ — the handful of constraints that are genuinely load-bearing: American-football-only scope, secret handling, no model weights in git, pgvector as the only vector store, single-camera capture, and the model-router contract. Load before adding an external resource, touching pipeline routing, or wiring a new secret.
version: 2.0.0
---

# Football-IQ repo guardrails

This is a short list of constraints that are expensive to get wrong. It is not
a process manual. If something you want to do is not named here, use your
judgement and the surrounding code's conventions.

## When to use this skill

Load it when you are about to:

- Add or change an environment variable, secret, model weight, dataset, API
  client, or third-party dependency.
- Change pipeline routing (`gpu-worker/pipeline/model_router.py`,
  `gpu-worker/pipeline/model_routing.json`, `docs/model-routing.md`).
- Propose an external resource — especially anything matching "football",
  which overwhelmingly returns soccer results.

Skip it for ordinary feature work, UI changes, refactors, and doc edits.

## Services

| Service | Role |
|---|---|
| **Backend API** (`backend/`) | FastAPI. REST API, auth, job queue, storage facade, nightly scheduler. |
| **Frontend** (`frontend/`) | Next.js static export. Coach-facing UI. |
| **Pipeline worker** (`gpu-worker/`) | Claims jobs, runs the CV stage chain, writes results back through the API. |
| **Postgres 16 + pgvector** | Relational store, similarity vectors, **and** the job queue (`processing_jobs`, `FOR UPDATE SKIP LOCKED` leases). |

**Hosting choice (human-approved): Cloudflare + Vercel.** The edge Worker in
`workers/api-edge/` (backend as a Cloudflare Container, R2 buckets,
Hyperdrive) and Vercel for the frontend are the sanctioned deployment wiring
— see `docs/setup/cloudflare.md` / `docs/setup/vercel.md` and
`.github/workflows/deploy.yml`. Credentials still never live in the repo
(wrangler secrets + Vercel env vars only), and no other provider-specific
wiring should be added without a human asking for it. Local-first defaults
remain: everything runs with no provider at all.

Boundaries worth keeping:

- Only the **backend** talks to third-party data APIs that require a key
  (CFBD, Kaggle, etc.). The frontend must never see those secrets.
- Only the **pipeline worker** loads model weights and runs inference. The
  backend never imports torch / ultralytics / RTMPose.
- The frontend holds no long-lived credentials — short-lived JWTs and
  presigned URLs from the backend only.

## Model-router contract

Pipeline routing is centralised in `gpu-worker/pipeline/model_router.py`. Every
pipeline stage **must** route through it — do not hard-code variant strings
inside stage modules.

Public API (do not break these signatures):

- `select_model(stage: str, priority: int) -> str` — returns the variant id
  for a stage at a given job priority. Unknown stages return
  `UNKNOWN_STAGE_FALLBACK` and log a warning rather than raising.
- `build_routing_artifact(stage: str, priority: int) -> dict[str, str]` —
  returns `{stage: variant}` so the dispatcher can merge it into
  `processing_jobs.output_artifacts["model_routing"]` for the audit trail.
- `is_same_session(priority)`, `is_nightly(priority)`,
  `is_nightly_only_variant(variant)`, `reload_routing()` — stable helpers.

Stages currently routed: `segment`, `calibrate`, `detect`, `ball`, `track`,
`reid`, `pose`, `render`, `embeddings`. New stages must be added to
`DEFAULT_ROUTING` with both `same_session` and `nightly` entries.

Priority buckets (defined in `queue/same_session_queue.py`):

- **Same-session — priority `10` (`SAME_SESSION_PRIORITY`)**. Period-break
  clips that must fit the 5–10 minute coaching feedback window. Routes to the
  fast variant.
- **Nightly — priority `0` (`NIGHTLY_PRIORITY`)**. Heavier variants allowed.

`NIGHTLY_ONLY_VARIANTS` is a **hard guardrail**: variants in that frozenset
(currently `sam3.1`, `sam3-mask-tracker`, `play-embed-clip-vitb32-baseline`,
`botsort`, `strongsort`, `parseq-ocr`) are blocked from the same-session bucket
even if a `MODEL_ROUTING_CONFIG` override tries to place them there — the
router replaces them with the default same-session variant and logs
`model_router_blocked_nightly_only_in_same_session`. When adding a new heavy /
experimental / token-gated variant, add it to `NIGHTLY_ONLY_VARIANTS`.

Every completed pipeline stage must persist its routing decision into
`processing_jobs.output_artifacts["model_routing"]` via
`build_routing_artifact` — that is how we prove after the fact which variant
served a given job. Tests that mutate `MODEL_ROUTING_CONFIG` must call
`reload_routing()` (or reload the module); the table is resolved at import.

See `docs/model-routing.md` for the full table.

## Secrets

1. **Backend-only by default.** If a key is read by the backend, add it to
   `backend/app/config.py` `Settings` as a typed field with a safe default
   (usually `""`) and document it in `.env.example`.
2. **Never expose secrets to the frontend or client bundle.** No
   `NEXT_PUBLIC_*` for API keys.
3. **Never commit a populated secret value.** `.env` is gitignored; only
   `.env.example` lives in the repo, with empty or placeholder values.
4. **Do not log secrets.** Redact at the structured-logging layer when in
   doubt. Do not echo env vars into CI logs or test fixtures.
5. **Rotate-friendly.** Do not bake a value into code paths, tests, or
   migrations.

Currently wired: `CFBD_API_KEY` / `CFBD_BASE_URL` (College Football Data,
backend-only; CFBD calls degrade to cached Postgres rows when unset).
`KAGGLE_USERNAME` / `KAGGLE_API_TOKEN` exist for the offline NFL Big Data Bowl
adapter and are not read by the backend.

## Soccer / association-football denylist

Football-IQ is an **American football** platform (Toledo Rockets, MAC,
NFL-style analysis). Soccer / fútbol resources are **rejected**, even when the
upstream package or dataset uses the word "football". Do not add these as
dependencies, ingestion sources, training data, benchmarks, or documentation
examples without an explicit override recorded in an ADR:

- `worldfootballR` — soccer R package (FBref / Transfermarkt / Understat).
- **SoccerNet** — soccer broadcast video benchmark.
- **FBref / Transfermarkt / WhoScored** scrapers — soccer data.
- Generic **StatsBomb open data** — soccer event data. (StatsBomb *American
  Football* is a separate product; treat it as a brand-new resource and run
  the full rubric.)
- **football-data.org** — soccer API despite the name.
- **SportMonks** football / soccer APIs — soccer unless a separately verified
  American-football product is proposed.
- Generic **FIFA / UEFA / European league** datasets — soccer.

Before adding any external resource that mentions "football", check it against
this list and the rubric in `docs/external-resource-rubric.md`, and add a row
to `LICENSES.md` for any new model or library dependency.

## Hard rules

These are the ones worth stopping over. If a task seems to require breaking
one, raise it rather than working around it.

- **No secrets in code, tests, fixtures, logs, or commit history.**
- **No model weights in git.** Weights are fetched at runtime or mounted; do
  not commit `.pt`, `.onnx`, `.bin`, or large `.safetensors` files. Update
  `LICENSES.md` when adding a new upstream model.
- **No new vector database.** Similarity search uses Postgres + pgvector (see
  `docs/embeddings-architecture.md`). Do not introduce Pinecone, Weaviate,
  Qdrant, Milvus, FAISS-as-a-service, or any parallel vector store.
- **No multi-camera assumptions.** The capture protocol is single-camera
  (`docs/capture-protocol-v1.md`). Do not add code paths that assume synced
  multi-camera rigs or that fail when only one camera is present.
- **No duplicate SAM integration.** SAM 3.1 lives **only** behind the
  `ENABLE_SAM3_NIGHTLY` env flag in `model_router.py`, routed for `detect` and
  `track` nightly buckets. Do not add a second SAM call site, a same-session
  SAM variant, or a parallel masking pipeline.
- **No mock data presented as real.** Synthetic detections, fake tracking IDs,
  fabricated CFBD rows, and placeholder embeddings must be labelled at the
  data layer and in any UI surface that renders them. Never wire a `mock_*`
  fixture into a production code path or a coach-visible endpoint.
- **Do not bypass the model router.** Stages call `select_model` rather than
  reading variant strings from env vars or hard-coding them.

## Testing

Run the suites for whatever you touched. Commands live in
`README.md` and `docs/runbook-local.md`; `.github/workflows/ci.yml` is the
authoritative list of what CI gates on (lint, typecheck, unit tests, the
Alembic up/down round-trip, and the cross-service integration job).

Note for local runs: several backend suites and the integration job need a
scratch Postgres 16 with pgvector, and the pipeline suite needs `ffmpeg` on
`PATH`.
