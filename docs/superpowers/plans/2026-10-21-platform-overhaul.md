# Football-IQ platform overhaul implementation plan

**Goal:** Implement the supplied twenty-page specification with independently reviewed, testable packages and evidence-backed release gates.

**Architecture:** Retain Next.js, FastAPI, Postgres/pgvector queue, R2 and the existing staff processor. Extend current resources. Preserve originals, corrections, leases and version fencing. Keep independent local operation supported.

**Spec:** `docs/platform-overhaul-spec.md` (verbatim user attachment).

**Global constraints:** Unknown evidence stays unknown. No synthetic production results, committed secrets or model weights. Interaction and prediction publication requires actual held-out evaluation. Production processor remains the staff GPU for the hosted deployment; local CPU operation remains supported.

**Current status (2026-10-21 continuation): incomplete.** The current per-page
implementation and missing-evidence ledger is
[`reports/overhaul/spec-coverage.md`](../../../reports/overhaul/spec-coverage.md).
Later continuation reports supersede the historical snapshots below. In
particular, explicit pressure-duration/corridor/envelope/role and reviewed-outcome
producers, original-five comparisons, signed feature exports and stored model
diagnostics are now implemented; trained automatic specialist coverage and
real-football acceptance are still separate requirements. The original pending
hash catalog is preserved alongside full verified hashes for all 36 sources.
No package checkbox denotes full acceptance without the missing external gates.

## Packages and ownership

- [ ] P0 Backend: durable processing intent, release inventory; record production and benchmark evidence gaps.
- [ ] P1 Worker/frontend: versioned validated derivatives; format-aware segmentation; elapsed-time track expiry; bounded same-shot overlay interpolation (200 ms gap, 100 ms endpoint freshness); original fallback.
- [ ] P2 Worker/backend/frontend: safe team-scoped identity, duplicate jerseys; persistent film workspace, coach/analyst layouts, URL context, server saved views, playback renewal and navigation continuity.
- [ ] P3 Backend/frontend/worker: one eligibility contract, canonical denominators, metric definitions and provenance; cohort query, distributions and film evidence; distinct position groups.
- [ ] P4 Specialist interfaces and observed geometry with explicit evidence requirements. Qualify interactions only with measured annotation results.
- [ ] P5 Predictive evaluation and baseline comparison. Publish predictions only when evaluation meets the specified gates.
- [ ] Verification: targeted regressions, full available unit/lint/type/build checks, database migration round trip, real API-to-worker-to-film workflow, browser task tests, independent review and fixes.

## Review focus

Long gaps and shot changes must hide markers. Duplicate/same-number opponents must not receive Toledo identities. Alternate views/fragments/retries must not inflate canonical plays or reps. Saturated/disconnected processing must retain upload intent. Failed replacement results must not erase usable prior output. All filters, saved views, drilldowns, angle switches and Back retain applicable context. Missing models and labels remain explicit, never fabricated.

## Evidence ledger

### Continuation after PR 57 merge

2026-10-21: User reaffirmed full-overhaul completion. Resumed on branch
`codex/overhaul-completion-20261021` from merged `7bb3793`. Existing implementation
and accepted test evidence remain intact; package checkboxes remain acceptance
gates, not claims that the prior engineering work is absent.

Ruling: use disjoint worker/frontend/release ownership concurrently and retain
the independent reviewer throughout, as requested by the user. No shared-file
edits without coordination. The source branch is isolated from `main`; no new
worktree, shared-branch push, merge or deployment has been performed.

Continuation ownership and interfaces:

| Work | Owner | Interface and verification |
| --- | --- | --- |
| Production recovery and independent reviews | reviewer | Actual serving SHA, readiness, placement and heartbeat; no credential reuse or production mutation. |
| CI/release readiness and image retention | root | Exact SHA + DB readiness; verified digest history; fail-closed pruning; transient rollout retries. |
| Format-aware sequence and association proposals | pipeline | Source PTS, complete shot/context retention, unresolved proposals never become accepted snap counts. |
| QB/OL/signed-gain producers | backend agent | Qualified events/coverage/geometry; additive evidence packets; no fabricated body extents or pressure coverage. |
| Event, release and gap film-linked views | root | Consume qualified packets and source timestamps; exact clip-relative navigation; accessible tables. |

Ruling: the observed production connection exhaustion does not establish database
capacity. The deployment recovered on its own. Do not guess pool defaults or
restart a healthy service. Add release checks; capacity remains an explicit
diagnostic question if exhaustion recurs.

Production recovery: independent provider checks found frontend, edge and backend
on the merge SHA, current worker heartbeats returning 200, and DB readiness 200.
The new public readiness script also passed three consecutive exact-build checks
at 16:53:44 UTC. Production schema head and models still require authenticated
inventory; a heartbeat alone does not prove those. The rerun of existing live
smoke passed expected backend release, but its page crawl timed out because
`networkidle` waits on the intentional alerts SSE on every route. Replace that
wait with bounded finite API-request settling and retain actual response/error
checks. No production credentials were read or copied.

2026-10-21: User authorized implementation of the attached design and independent subagent review. Baseline checkout `1aad137`, branch `claude/no-local-copies`, clean worktree. Reviewer, backend and pipeline agents assigned disjoint ownership. Frontend/integration owned by primary agent. The supplied release gates require actual footage annotations and production access; code tests alone cannot satisfy those gates.

### Frontend, local workflow and staff labeling evidence (2026-10-21)

- Frontend engineering checks: **387 unit tests**, ESLint, TypeScript and production static export passed. **15 deterministic Playwright tests** passed. The shared-context controls now include named session pagination, personnel, formation, situation, drill, contact and opponent. Definitions errors expose retry. Player profiles work against the local API and preserve safe return/cohort navigation, while their lifetime/official/physical scope remains explicit.
- Real local API/media checks passed saved-view create/read/update/delete, definitions, cohort/exclusion responses, play evidence, clean proxy full decode, durable report download and component inventory (`reports/overhaul/workspace-verification.json`). The actual cohort had **zero eligible measurements**: the 107 experimental/unknown measurements were excluded, rather than represented as trusted football analytics.
- Real Chromium playback advanced 1.117 seconds in a 1.1-second wall interval at 1×. The calibrated field view is inside the video stage, defaults to a quiet fixed bottom-right inset of about 22% width, hides labels/controls at rest, and has working expand/move controls. Layout changes preserved time, tablet geometry had no horizontal overflow and no browser JavaScript errors occurred (`browser-verification.json`, point-in-time values may vary with fresh runs). Exported worker maps use the same restrained intent.
- The real disposable-library structural lifecycle passed split → merge children → undo merge → undo split. A real CPU worker reprocessing job preserved exact staff clip IDs/windows; original deep links remained available, original labels/boundaries were restored and edit history remained (`boundary-workflow-verification.json`). This verifies application behavior, not whether inferred football boundaries were correct.
- User requested a staff labeling package after confirming no annotation export. Delivered source catalog for all 36 recordings, 36 empty drafts, 108 unassigned reviewer/pass rows, codebook, source grouping and split templates, draft-only local viewer, native-frame PTS indexer, bounded source hashing and explicit human freeze/adjudication tooling. The thirty April 16 practice extracts propose one source group; six game-file groups still require verification. The delivered package has no confirmed groups, completed labels or benchmark approvals.
- The local labeling tool passed **5 core tests** and real-source browser checks for actual presented PTS, exact desktop/small-screen box geometry, occlusion records, draft export/restore and independent pass storage. All automation-entered labels stayed unreviewed test input. The frame indexer refuses unknown/nonzero media clock origins and rotated media pending proven normalization; three actual FFmpeg regressions cover fractional PTS, existing-output preservation, source dimensions and nonzero-origin refusal. Full `tests_local` passed **55 tests**.
- `Football-IQ-Staff-Labeling-2026-10-21.zip` contains 62 metadata/tool/documentation files and no film or model weights. Archive CRC and every internal SHA-256 were verified. Fresh extraction passed the 19 package/freezer tests and five annotation-core tests (`labeling-bundle-verification.json`).
- Edge checks passed **142 tests** plus TypeScript. The live production inventory was inspected read-only; this diff has not been deployed. See the independent page-by-page `reports/overhaul/spec-coverage.md` for unfinished implementations as well as missing evidence. Full-platform acceptance remains incomplete.
- Final backend snapshot passed **796 tests** (8 deprecation warnings), Ruff formatting for 204 files, Ruff lint and mypy for 111 modules. A fresh isolated database completed base → 0010 → base → 0010 with no Alembic drift. Final review closed stale output-ID mutation, partial/zero-track replacement holds, premature detailed-tier badges and pending track-label leakage; prior qualified complete versions and staff annotations remain coherent. The local API was restarted with that source and its real workspace/browser smoke passed again.

### Worker evidence and remaining contracts (2026-10-21)

- Worker engineering verification: **1,362 passed, 1 skipped, 1 integration deselected** in 32.21 s after the final staff-boundary shot-audit and post-cut calibration guards; Ruff and worker diff checks passed. These are regression results, not measured football accuracy. The small checked-in evidence summary is `reports/overhaul/canary-summary.json`.
- The separate actual CI cross-service test passed (**1 passed in 20.24 s**) against scratch database `footiq_overhaul_cross` at localhost:55432 after migration through `0010_clip_edits`. It exercised real API upload, queue claim, worker writes, clean H.264 media and pending-synthetic quarantine. Windows backend-interpreter lookup, stale synthetic read expectations and repeated-run source-identity isolation were fixed in the fixture; no application qualification filters were weakened. `reports/overhaul/cross-service-verification.json` records this synthetic wiring evidence, which does not qualify football accuracy or production deployment.
- Actual RTX 5060 Laptop/CUDA canaries processed the supplied 10.3103 s practice source in 31.05 s and an accurately cut 30.013317 s game excerpt in 58.03 s. Both retained 100% of source intervals, and all five proxies passed encoded-duration and source-picture alignment checks. The outputs were playable; neither qualified trusted analysis. The practice baseline produced 95 fragmented tracklets; the game baseline produced 226 and three unresolved shot windows. These counts are not ground-truth quality measurements or the required five-minute/90-minute timing tests.
- Optional BoT-SORT wiring was executed on actual practice frames: 2,685 torso HSV descriptors, 308 measured background ECC pairs, zero unavailable pairs, 51 tracklets in 14.015 s (`reports/overhaul/tracker-input-canary.json`). This proves real adapter inputs, not improved tracking accuracy or learned identity quality. Comparing its whole-source tracklet count with per-clip baseline counts would be invalid.
- Immutable media generations now bind source/window/configuration and require timing/alignment validation before publication. The latest quiet `pts-overlay-v4` real-source export passed duration validation (3.733333 s encoded for a 3.737067 s source window). The calibrated field inset correctly remained disabled on this uncalibrated source; meaningful geometry, canonical-coordinate, same-shot/gap and qualification tests passed. Live calibrated-film acceptance remains unestablished.
- The worker consumes `/roster-assignments/for-video/{id}` with a 404-only compatibility fallback, preserves duplicate scoped candidates, and requires resolved team membership on games/unknown sessions. Automatic candidates remain provenance only because `auto_identity_approved` defaults false; candidate machinery is implemented, while held-out identity precision/coverage and promotion remain unmet.
- Manual split/merge/undo integration uses authoritative `/videos/{id}/boundary-overrides`, preserving returned clip IDs and source-time windows on reprocessing. Non-404 lookup failures stop segmentation instead of replacing staff edits. A final correctness fix retains a decoded-source shot audit even with manual windows: actual practice starts `[0]` and game-excerpt starts `[0, 0.6, 25.2]` were measured in 1.887 s and 1.565 s, respectively; staff windows stayed identical and the cross-cut merged game window correctly lost spatial eligibility (`reports/overhaul/manual-shot-audit-canary.json`). Unknown merged views cannot inherit a recording-wide transform; two new regressions cover track resets, per-clip spatial suppression, unaffected single-shot windows and no-frame audit failure. Independent review reported UI 50 ms versus API 100 ms split limits, stale boundary-form state, legacy Skill group mappings, and split/merge loss of known view metadata to the responsible owners; their final fixes and checks are tracked in the independent review report. RB/WR/TE summary/payload/editor compatibility passed 29 affected frontend tests, targeted ESLint and TypeScript checking.
- Substantive remaining worker implementation gaps: general format-aware snap/live-action/replay/alternate-angle association is not implemented by the conservative unresolved-shot fallback. Clear-lane body extents, protection/contact/responsibility specialists and trained expected-outcome models remain absent or experimental. Existing signed-gain/contact heuristics do not meet the specified signed football outcome definitions. QB time-to-first-pressure currently requires an actual qualified onset; explicit continuous pressure-observation coverage and censored TTFP production are not implemented, and a snap-defined 200 ms tackle-box-residence producer is absent. Silence from a pressure detector must not be treated as pressure-free observation. Registering definitions, drawing charts or passing unit tests does not close these gaps.
- No annotated IDF1/HOTA, visible-player recall, spatial error, identity confidence-bound, interaction accuracy, predictive calibration, representative staff-GPU throughput or complete production upload-to-result gate is established by these engineering canaries. Full-platform acceptance remains **not ready** until the missing implementations and evidence gates are satisfied.

### Staff labeling preparation (2026-10-21)

The user requested a staff labeling package. `scripts/prepare_overhaul_labeling.py`
now generates the 36-source register, 36 empty annotation drafts, 108 unassigned
primary/secondary/adjudication rows, empty split plan, target/gap summary and an
unreviewed dense-coverage scaffold under `reports/overhaul/labeling-package`.
The catalog retains actual probed dimensions/rational frame rates; all delivered
source hashes are pending and group/category confirmation remains staff work.
Thirty April 16 practice extracts propose one group and the six game files
propose six more, with game format unresolved. Zero groups are confirmed; at
least five additional independent groups are needed even if all seven proposals
prove valid, with category balance potentially requiring more.

The helper streams original media SHA-256 in 1 MiB chunks and refuses overwriting
existing files, including original hardlink aliases. An actual 8,666,433-byte
practice source and 3,742,691,239-byte full game were hashed in 3.751 s total
(`reports/overhaul/labeling-hash-canary.json`). This establishes hashing execution,
not source independence, annotations or accuracy. Freezing requires explicit human
review and confirmed source context, checks actual timestamps/boxes/target-linked
identity evidence, preserves unknown identities and non-countable sequences, and
requires separate independent passes covering all selected canonical occurrences
before claiming double annotation/adjudication. It preserves supplied dense
coverage references without manufacturing human coverage attestations.

Nineteen labeling regressions and targeted Ruff passed. Instructions and the
codebook live under `docs/labeling`; the standalone viewer/native frame-index
tool and release evaluator have separate owners and independent review. No
human labels, dense-coverage truth or release measurements were generated.
