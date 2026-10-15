# Local Football-IQ revamp

This implements the approved local-first direction for staff Windows/macOS
laptops without dedicated GPUs. Independent local libraries work first. The
existing Cloudflare Hyperdrive/PostgreSQL, R2 and hosted API wiring is preserved;
connecting a laptop worker to that API remains optional. No hosted migrations,
data changes or deployments were performed during this implementation.

## Implemented workflow

| Area | Result |
|---|---|
| Local application | Native launcher/builder, owned PostgreSQL with pgvector, static UI, API, storage and CPU worker; private install configuration and one-use browser sign-in |
| Film organization | Persisted practice/game sessions, camera-labelled independent recordings, canonical plays and manually matched views; matched views do not inflate play counts |
| Import and playback | Bounded real uploads with finalization/probing, seekable originals, clean H264 clips, explicit VFR source/proxy timing, honest unavailable/partial processing |
| Processing | Review/Quick/Detailed CPU profiles, model readiness, real detector/model hashes, durable queue leases and fencing, pause/resume/cancel/retry and revision-aware checkpoints |
| Coach review | Immediate boundary, identity, formation, route, coverage, personnel and event edits; revision conflicts, retry-safe operations, history, review completion and reprocessing |
| Roster | Stable player IDs, add/edit/active status and CSV preview/apply with conflict validation |
| Reports and transfer | Durable scoped downloadable reports and versioned session ZIP export/import, actual media hashes, idempotent replay and explicit preserved-work conflicts |
| Self-healing | Bounded API/worker restarts, health/status, private stop controls, Windows-safe locking and shutdown, targeted repair, checkpoint validation and pre-migration owned-database snapshots |
| Local learning | Actual reviewed-label JSONL and feature cohorts, disjoint session splits, real CPU coverage fitting, persisted predictions, immutable human evaluation, hash-bound promotion and rollback |
| UI | Today inbox, session-led Film Room, raw review before inference, Players, Scouting, Reports and live This computer status; removed inert settings and implicit mock/offline-success paths |

Learning currently trains the coverage classifier, not the player detector or
every tactical model. It requires at least 30 training, 10 calibration and 20
holdout plays with supported human labels and usable same-clip Detailed features.
Insufficient or stale observations block cohort creation. Holdout performance
and an administrator's action determine release; automatic heavy training is off.

Field measurements depend on calibration and provenance. Unknown, synthetic,
superseded or unvalidated outputs cannot become trusted coaching measurements
through a display toggle. Missing weights skip optional analysis or yield an
actionable partial result; they never fabricate observations.

## Verification

Verification performed in the Linux execution environment:

- Backend: 667 tests passed; strict mypy passed across 96 application files;
  Ruff lint/format passed. A fresh disposable PostgreSQL database completed
  migration upgrade → downgrade → upgrade.
- Frontend: 37 files / 247 tests passed after the authentication fixes; lint,
  typecheck and production static export passed. Live browser sign-in, Film
  Room, raw playback, computer status and insufficient-label preview produced
  no console errors or failed requests. All 14 deterministic Chromium browser
  tests passed.
- Worker: 1,172 CPU tests passed and one was skipped. The separate tagged
  API/PostgreSQL round-trip test passed, including source bounds, actual clean
  H264 media and exclusion of synthetic results from coaching overlays.
- Cloudflare edge: typecheck and 122 tests passed, including signed streaming
  and multipart upload behavior. Existing hosted credentials/connectivity were
  not available for a live deployment test.
- Real repository drone footage completed on two CPU threads with actual YOLO
  weights, persisted tracks and clean/annotated H264 media. The live API flow
  passed import, range playback, CPU processing, immediate correction, review
  completion and download of actual scoped report bytes. No reliable snap was
  observed in the example, so route/coverage analysis abstained.
- A frozen distribution initialized its own native PostgreSQL/pgvector library
  and passed the full Quick workflow on repository drone recording 0284 with
  actual YOLO nano CPU inference: clean playback, 49 persisted tracks, immediate
  correction, review completion and downloadable report bytes. It recovered
  after separately terminating the worker and API and preserved work across a
  full library restart. Review preparation without models also passed. No snap
  was observed, and these checks do not establish football accuracy.

## Release limits and next evidence

Windows and Apple Silicon macOS packaging/build smoke workflows are implemented
in `.github/workflows/native-app.yml`. They must run on those target systems;
they were not executed in this Linux environment. Native GUI/hardware trials,
Windows signing and Apple notarization remain release validation work. Bundled
dependency notices are retained; redistribution must include the exact required
native binary sources/notices.

Session archives support sharing between independent libraries. Automatic
concurrent multi-laptop synchronization and automatic camera synchronization or
triangulation are not implemented. A connected CPU worker can use the existing
team API once that API is updated with these routes; this implementation did not
deploy it or alter the Cloudflare database.

Actual accuracy and throughput need representative Toledo footage, reviewed
ground truth and staff hardware measurements. A successful job, a complete UI,
or a track count cannot establish superiority to Hudl or SkillCorner. The app
now has a working path to collect and evaluate that evidence.

Start with [the local runbook](runbook-local.md),
[CPU model/readiness guidance](setup/local-worker.md),
[local learning](cv/local-cpu-learning.md), and
[ADR 0006](adr/0006-local-multi-recording-review-and-learning.md).
