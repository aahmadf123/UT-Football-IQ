# Second revamp: verification and rollout

Verified October 15, 2026, using an isolated local database and storage. No
production login, CFBD key, or other production credential was used or committed.
Architecture and product limits: [ADR 0007](adr/0007-shared-hosted-cpu-film-room.md).

## User workflow

| Boundary | Result |
| --- | --- |
| Browser upload → API → object storage → recording/session | Passed with the repository's actual Toledo drone film |
| Second signed-in account → shared library → video playback | Passed |
| Viewer upload and processing permissions | Both rejected with HTTP 403 |
| Administrator role assignment → fresh Coach session → processing access | Passed; role restored through the UI afterward |
| Concurrent byte-identical uploads from different staff accounts | One recording created; seven reused the same ID; existing session/tags/camera preserved |
| Duplicate response → browser upload status | Shows Already in library and Open recording |
| Model-free Review → saved clip → browser playback | Passed without invented tracking |
| Real pretrained CPU detection → persisted tracks → synchronized video boxes | Passed on actual football footage |
| Box selection → player inspector; original/overlay toggles; full-recording overlays | Passed |
| Correction persistence and reviewed flag | Passed; synthetic wiring corrections excluded from training |
| Desktop 1440px and mobile 390px | No page errors or horizontal overflow across dashboard, library, clip and recording views |
| Migration 0005 upgrade/downgrade/upgrade | Passed in a separate scratch database |

The actual original recording `Dji 20260416111539 0284 D.mp4` is 8.575 seconds,
1280×720. The CPU Quick job completed in 293.27 seconds during this test session,
decoding 257 frames, sampling 86 detection frames and persisting 49 track segments.
Moving SVG coordinates and selection were checked at different playback times.
Segments can fragment and identities remained unassigned. This is workflow
evidence, not an identity-accuracy benchmark, real-time throughput promise or
Hudl/SkillCorner accuracy comparison. Dedicated ball and pose models were absent;
those layers remained unavailable.

## Automated checks

- Frontend full suite: 38 files, 268 tests passed. Subsequent focused regressions
  for season-response ordering and team access passed. Lint, typecheck and the
  production static export passed.
- Playwright Chromium suite: all 14 tests passed using system Chromium. These
  deterministic mocked API tests complement the real local workflow above.
- Backend full suite: 695 tests passed against a separate PostgreSQL/pgvector
  database. Later role-concurrency and bootstrap regressions passed. Ruff,
  formatting and strict application Mypy checks passed.
- Worker full suite: 1,187 passed, one skipped, one deselected. Additional hosted
  limit/registration regressions passed. Worker Ruff passed.
- Edge Worker suites: 130 tests passed, including container environment and
  activity handling.
- CPU Docker image built successfully with installed CPU Torch/Ultralytics and
  checksum-verified pretrained weights. The non-root hosted lifecycle smoke
  passed migrations, first-human-admin preservation, safe service-account
  bootstrap, real inference warmup, worker registration with limits,
  authenticated idle checks, and worker shutdown before API shutdown.

## Rollout

Use the existing [Cloudflare](setup/cloudflare.md) and [Vercel](setup/vercel.md)
deployment wiring. The new image runs schema migrations before serving. Keep the
database, R2, signing, scheduler and dedicated worker-account secrets configured;
configure `CFBD_API_KEY` privately on the Worker for administrator refreshes.
Ordinary CFBD reads remain cached and the browser never receives the key.

The hosted CPU worker accepts up to 512 MiB, five minutes and 1080p per recording,
with at least 4 GiB temporary free space. Larger originals remain in the library
for review. Historical records without a fingerprint are not automatically merged;
their fingerprints are populated when re-registered. Duplicate media objects are
preserved to avoid deleting existing references.

Live deployment and live provider testing were blocked in this chat: its Vercel
connection required reauthentication, the selected workspace did not expose
Cloudflare/PlanetScale credentials, and its HTTP policy blocked the production
and reference sites. The live hosted application was not changed. CFBD upstream
refresh was verified with controlled tests, not a live vendor request.
