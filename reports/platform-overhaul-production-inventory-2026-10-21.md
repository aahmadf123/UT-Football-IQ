# Platform overhaul: independent production inventory

Read-only observations collected on 2026-10-21 by the independent review agent.
No deployments, configuration changes, database writes or production uploads were performed.
Timestamps below use UTC. This is an inventory, not an end-to-end acceptance result.

## Verified deployed components

| Component | Observed state | Evidence |
| --- | --- | --- |
| Vercel production frontend | `READY`, `dpl_3Qo46yh1or7sPHrhZPtWvpKm87tM`, created `2026-10-21T10:20:07.726Z`; `main` SHA `d1f4317bae5529864bd2d9df80713515dd5d9891` | Vercel `get_deployment` with known project/team scope |
| Production alias | `https://ut-football-iq.vercel.app` is assigned to that production deployment and returns HTTP 200 | Vercel deployment aliases and independent HTTP GET |
| Build-time API origin | Production JavaScript chunks contain `https://footiq-api-edge.firas-azfar.workers.dev` | Downloaded production HTML and its 15 script URLs; two scripts contained that origin |
| Latest preview | `READY`, `dpl_H5KZ3DjPfAVAgqE14iwuNrsFiitn`, SHA `1aad1377eafb4f00069e6eea359a3ff622f79a80`, URL `https://ut-football-cplnysxq2-aahmadf123-7151s-projects.vercel.app` | Vercel project/deployment inventory, newest five deployments only |
| Vercel runtime setting | Node `24.x`; project framework `nextjs` | Vercel `get_project`; local CI uses Node 22, so the environments differ |
| Cloudflare edge | `footiq-api-edge`, deployment `8193c544-cc27-4618-9435-bafea9727a37`, 100% version `46998d80-9173-4c68-94c3-21acfbd46bee`, created `2026-10-21T10:20:38.329266Z` | Cloudflare Workers deployments API, newest three deployments only |
| Edge release SHA | `d1f4317bae5529864bd2d9df80713515dd5d9891` | Allowlisted public `GIT_SHA` plain-text binding |
| Hosted inference | `HOSTED_PROCESSING_ENABLED=0` | Deployed Worker settings; staff processing remains separate |
| API container | Application `footiq-api-edge-backendcontainer`, version 39, one instance and maximum one instance | Cloudflare Containers applications API |
| Container image | `registry.cloudflare.com/4607053406491c18132943949b972140/footiq-api-edge-backendcontainer@sha256:b8fcb4aacde01202a9c7144d6e56e38c377d5b759c0ae997ee42ced66d276d6b` | Current container configuration; digest-to-source-build association still needs verification |
| Object storage | `footiq-raw-video`, `footiq-clips`, `footiq-overlays`, `footiq-artifacts` exist and their corresponding Worker bindings exist | Cloudflare R2 buckets and Worker settings APIs |
| Database wiring | `BACKEND_DATABASE_URL` secret and `HYPERDRIVE` binding exist | Names/types only; secret values were not decrypted or printed; binding presence does not prove container database use |
| CFBD configuration | `CFBD_API_KEY` secret binding exists | Name/type only; authenticated CFBD request and cache freshness remain unverified |
| Startup user seed | `SEED_USERS_ON_STARTUP=false` | Deployed Worker public setting |

The current and one previous production Vercel deployment appeared as rollback candidates in the newest-five listing. Older deployable images were not exhaustively inventoried. No rollback was performed.

## Public HTTP and CORS observations

Requests used a browser User-Agent and the canonical frontend Origin.

- `GET /edge/health`: HTTP 200, production status `ok`, canonical CORS origin present.
- `GET /health`: HTTP 200, service `football-iq-backend`, environment `production`, canonical CORS origin present.
- `GET /api/v1/system/release`: HTTP 404 on the currently deployed pre-overhaul build.
- `GET /api/v1/system/version`: an initial 15-second timeout was followed by HTTP 200 with SHA `d1f4317bae5529864bd2d9df80713515dd5d9891`, independently matching the edge setting and Vercel production SHA.
- `GET /ready`: an initial timeout was followed by HTTP 503, then HTTP 200 with `checks.database=ok`. Database reachability succeeded on the final probe, but the observed intermittent readiness requires follow-up under the real upload workflow.
- Initial requests without browser User-Agent/Origin received HTTP 403. Subsequent requests with the browser context above succeeded for the two health endpoints.

Confirmed CORS defect on `OPTIONS /api/v1/auth/login` using requested method POST and headers authorization/content-type:

| Origin | Status | Access-Control-Allow-Origin |
| --- | --- | --- |
| `https://ut-football-iq.vercel.app` | 204 | Canonical origin |
| `https://ut-football-cplnysxq2-aahmadf123-7151s-projects.vercel.app` | 403 | Absent |
| `https://ut-football-iq-git-main-aahmadf123-7151s-projects.vercel.app` | 204 | Branch alias origin |

The deployed regular expression allows `ut-football-iq-*` previews, while Vercel's actual unique deployment URL uses `ut-football-*`. A preview can therefore load its frontend but fail API preflight. The local configuration now accepts both actual prefixes only within the exact observed team suffix; tests read the real configuration and cover permitted URLs, unrelated teams/projects and lookalike hosts. No production setting was changed during this inventory, so the live defect remains until that reviewed configuration is deployed.

## Remaining release evidence

These are open acceptance requirements:

1. Authenticate through the application and trace an actual source upload, durable job intent, staff-GPU claim/heartbeat/result, signed Range playback, evidence, analytics and downloadable report.
2. Bind the staff worker's checked-out SHA, actual model weight hashes, GPU/VRAM/runtime and registered model versions to the same release. An edge/frontend deployment does not update that computer.
3. Confirm database schema head and deployed container build provenance without exposing credentials. A root `.env` contains a remote `DATABASE_SYNC_URL`; its target was not proven to be the production database and it was not queried for this report.
4. Verify original and derivative checksums, generator/timeline/encoder versions, normal-speed decoded playback, audio synchronization and early/middle/late alignment on real affected recordings.
5. Reconcile CFBD connectivity, selected season and roster against official 2026 sources; binding existence does not establish readiness.
6. Run the specified held-out 240-play/12-source benchmark, dense tracking and identity evaluation, interaction/prediction gates, and staff-GPU throughput tests. Stub/unit tests cannot substitute for these results.
7. Retain the current and two previous usable deployable images after verifying registry capacity and rollback compatibility.

## Tool provenance and access boundaries

Working inventory paths: `mcp__vercel` project/deployment tools and Cloudflare read-only GET APIs. The older `mcp__codex_apps__vercel` connection returned `UNAUTHORIZED` requiring reauthentication; the working replacement connector supplied the Vercel observations above. Public HTTP probes and downloaded public frontend scripts supplied the playback-origin/CORS observations.

No model accuracy, staff-GPU connectivity, signed media bytes, protected roster data or database readiness is established merely by green health responses.

## Local hardware and supplied-source inventory

The current machine has an NVIDIA GeForce RTX 5060 Laptop GPU with 8151 MiB
VRAM and driver 610.62. `gpu-worker/.venv-gpu` reports PyTorch `2.8.0+cu128`,
CUDA runtime 12.8, CUDA available and one GPU. Four existing local weight files
were hashed without loading or changing their bytes. The machine-readable
[local GPU/weights inventory](platform-overhaul-local-gpu-inventory-2026-10-21.json)
does not establish that this is the registered production worker.

The [source inventory](platform-overhaul-source-inventory-2026-10-21.json)
contains read-only ffprobe results for every file in the repository's `Drone
Footage` folder and the supplied `UT Game Film 2026` folder:

| Folder | Files | Total source minutes | Observed video | Audio |
| --- | --- | --- | --- | --- |
| Drone Footage | 30 MP4 clips | 6.15 | H.264, 1280×720, 30000/1001 nominal and average FPS | No audio streams |
| UT Game Film 2026 | 6 MP4 games | 563.32 | H.264, 1280×720, 60000/1001 nominal FPS; differing rational average rates | No audio streams |

All 36 metadata probes succeeded; originals were not moved or edited. No JSON,
CSV, XML, text or other annotation/reference files were present in either
folder. The 30 drone extracts share one timestamped April 16 sequence; their
file count does not establish 30 independent benchmark sessions. Independent
game/session grouping and film category still require human annotation.

This supplied set does not contain native 1080p benchmark footage, an audio-sync
sample or an identified television category. Upscaling 720p would not establish
native 1080p throughput or replace missing audio. The full-plan
[release gate report](platform-overhaul-release-gates.json) remains `not_ready`
until the actual labels, model evaluations, hardware benchmarks and complete
production trace are supplied through the documented evidence contract.
