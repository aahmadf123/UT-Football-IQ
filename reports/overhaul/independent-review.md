# Independent implementation review

Review date: 2026-10-21. Scope: the active uncommitted platform-overhaul diff,
the supplied twenty-page specification, source media metadata, live provider
inventory and the generated local verification artifacts. This review does
not approve production deployment or establish football-model accuracy.

Final reviewed scope has no known outstanding correctness blocker. This is
approval of the reviewed engineering changes, not full twenty-page completion;
the missing implementations and release evidence below remain open.

## Findings corrected during review

- Bounded overlay interpolation now hides long observation gaps, shot changes
  and explicit lost states. The trail no longer extends to future samples.
- Shot and visibility fields now survive track-point storage and overlay API
  serialization; fixture-only correctness was insufficient.
- Derivative generation validates duration and mapped source pictures, preserves
  source audio when available and uses immutable storage generations.
- Identity candidate narrowing respects resolved team membership, duplicates,
  concurrent conflicts and shot boundaries. Unapproved candidate IDs remain
  candidates rather than automatically published roster identities.
- New geometry sampling retains missing-coordinate/lost barriers and abstains
  on unknown irregular-timeline interpolation.
- The exported field inset now converts the actual calibration frame
  (goal-to-goal x 0–100, centered lateral y) into its full-field canvas. The
  original added implementation would have omitted the negative-y half.
- Metric and track replacement now use completion-time publication. Staff
  metric corrections are preserved. Human identity overrides bind only to an
  exact unique observation fingerprint; an ambiguous rerun keeps the previous
  complete result rather than silently discarding the correction.
- Legacy frontier reads use the common publication filter; scoped saved views
  enforce owner/team access, and malformed view updates return validation errors.
- Live unique Vercel preview URLs were rejected by deployed CORS. The local
  correction accepts the actual unique/branch prefixes only within the observed
  owning team suffix. Tests import the actual Wrangler configuration. Production
  was not changed, so deployment of that correction remains outstanding.

## Independent checks

- Edge CORS: 23 tests passed after first reproducing the actual rejected-preview
  and unrelated-team-acceptance defects. Edge type checking passed.
- Final engineering suites reported by the owners: backend 796 passed (eight
  deprecation warnings) in 81.42 seconds, formatting for 204 files, Ruff and
  mypy for 111 files passed; frontend 387 tests, ESLint, TypeScript, static
  production export and 15 deterministic browser tests passed; worker 1,362
  passed, one skipped and one integration deselected, with Ruff clean. The
  complete edge suite passed 142 tests and type checking. These regression
  totals establish the tested contracts, not held-out football accuracy.
- The actual scratch-database cross-service integration passed in 20.24 seconds:
  API upload/registration, queue claim, worker writes and H.264 derivative probing.
  Review of its fixture fixes confirmed Windows interpreter selection, per-run
  synthetic content isolation and persisted observation checks without weakening
  normal list/overlay quarantine. This synthetic wiring run establishes neither
  football accuracy nor production readiness.
- Release evidence evaluator: 23 tests passed, including missing evidence,
  tampering, split leakage, independent-source inflation, alternate-view
  identity conflicts, non-finite values and protection against overwriting
  source evidence. Further review rejected boolean measurements, incomplete
  tracking reports, boolean deployment versions and hard-linked output paths
  that could mutate original evidence. Independent cross-review also caught
  malformed-input self-overwrite and models referenced inside reports; both
  were fixed and re-reviewed. Native-frame dense coverage now requires a
  separate hashed actual frame index, human completion attestations, exact
  visible-target boxes and documented loss states; two sparse points cannot
  qualify a play as dense. The pipeline owner reported the expanded full
  local-runtime/labeling suite passed (55 tests), including three actual-FFmpeg
  frame-index cases for fractional PTS, offset-clock refusal and catalog/pixel
  dimension mismatch.
- Source inventory: all 36 read-only ffprobe probes succeeded. The 30 practice
  extracts total 6.15 minutes; the six games total 563.32 minutes. All are 720p
  H.264 without audio. Neither folder contains annotation files.
- The current execution GPU and PyTorch/CUDA availability were directly queried;
  four existing local weight files were SHA-256 hashed. These observations do
  not identify the hosted deployment's active staff worker.
- Live Vercel/Cloudflare metadata and public HTTP/CORS were independently
  checked. Production frontend, edge setting and backend `/system/version`
  agree on SHA `d1f4317bae5529864bd2d9df80713515dd5d9891`. Readiness was
  intermittent before a final successful database check.
- `workspace-verification.json` and `browser-verification.json` were inspected.
  They document actual local media decoding/1× playback and task wiring, with
  football accuracy explicitly unevaluated. The analytics cohort had zero
  eligible measurements; this must not be described as qualified analytical
  acceptance.
- Qualified position chart renderers now support a guarded QB timing/depth
  scatter, measured spacing series and actual calibrated field trajectories.
  Six component regressions, type checking, targeted ESLint and the installed
  ECharts SVG renderer passed. They do not create missing data: QB pairing
  requires matching player/trajectory/result/definition, and absent real series
  yields no chart. Signed dropback preserves advancement as negative depth and
  shares the producer's snap-to-release definition. Actual measured trajectories
  and original-pair spacing packets now have worker producers; publication still
  requires qualification evidence.
- Censored duration charts consume the backend's actual survival steps, preserving
  all-censored lower bounds. Five component regressions passed; missing or invalid
  curves render no invented curve. Only the three intended censored duration
  definitions are accepted, with accessible risk-set and source-evidence tables.
- Recording-review overlays are bound to the active clip ID, preventing stale
  track payloads from appearing across play boundaries. The compact in-video
  field suppresses gaps and lost/out-of-bounds positions; keyboard focus reveals
  its controls. The quiet header's bright-film contrast was corrected with an
  opaque backdrop and white focused controls before final browser verification.
- Player profiles now fetch local API identity without a build-time remote-origin
  flag, preserve shared context in peer/switch navigation, validate deterministic
  Back targets and expose a real filtered-film analysis link. Their existing
  lifetime metrics are explicitly labeled with that scope. Three mounted tests,
  targeted lint and type checking passed; the primary owner independently
  reviewed the change.
- A read-only-source staff labeling package now includes 36 unconfirmed sources,
  36 empty drafts, 108 unassigned review passes, separate adjudication/freeze
  instructions and a local frame-aware viewer. The actual-source browser canary
  records decode, exact displayed timestamps, scaled pixel boxes and draft
  backup/pass separation. Its automation-entered labels are test input, never
  human football truth. Source hashes stream in bounded chunks; frozen evidence
  needs explicit actual staff review. Native-frame indexing abstains when the
  source clock is unproven. Both video and container origins must be verified;
  actual pixel dimensions must match the catalog, and rotated media abstains
  pending a verified displayed-pixel conversion. The separately created archive
  includes only the listed code, documentation and metadata. Its CRC, every
  internal SHA-256 and fresh extraction were verified, with 19 labeling and five
  viewer-core tests passing. Neither the archive nor its empty templates supply
  human labels or media.
- The real disposable-library boundary workflow exercised split, merge, reverse
  nested undo and reprocessing through the public API. Its recorded job retained
  staff IDs/windows, restored original labels and deep links, and kept audit
  history. Cleanup uses safe reverse-order undo and preserves conflicts with
  later changes. This is application-wiring evidence, not football accuracy.

## Open acceptance and implementation gaps

The generated release report is **not_ready**, with 38 failed/missing gates
out of 40. An empty leakage set or syntactically valid empty manifest is not a
passing release. The evaluator checks frozen evidence and thresholds, not the
truth of human annotations or evaluation software.

The following require real evidence: 240 held-out canonical plays from twelve
independent source groups balanced across all three film categories, dense
tracking labels, double annotation/adjudication, approximately 2,000 accepted
identity cases, measured spatial/interaction errors, actual native-1080p and
90-minute staff-GPU timings, and a complete production upload-to-result trace.
The supplied source folders do not provide those labels, native 1080p, audio or
an identified television slice.

There are also substantive implementation gaps, distinct from external
verification:

- The game segmenter retains a narrow edited-film recognizer and a conservative
  unresolved multishot fallback. It does not yet demonstrate the complete
  format-aware event/replay/angle association system described on page 8.
- BoT-SORT now receives actual runtime camera-motion/appearance features, and
  roster assignments now support effective dates, session overrides and worker
  season/team-scoped candidate reads. These code paths still need complete
  held-out comparisons; canary fragmentation is not a passing tracking benchmark.
- Requested position products extend beyond the implemented guarded QB scatter,
  measured series and source path charts: release maps, gap-time heatmaps and
  blocker–rusher matrices,
  RB path/gap views, WR route/separation views, TE transitions, defensive
  pursuit/rush views and special-teams landing/coverage views still need their
  qualified producers and complete film-linked interfaces.
- Clear lane/protection-envelope/contact/responsibility specialists and trained
  expected-outcome models remain absent or experimental. Honest abstention is
  required; registering definitions does not implement or qualify the models.
- Unattended fast feedback now schedules an idempotent detailed pipeline job in
  the durable queue. A full real-media overnight lifecycle and measured detailed
  qualification remain unverified.

Same-clip saved-workspace restoration, strict same-site Back targets, distinct
gap-subject preservation and explicit trajectory/result origin now have code
fixes and regression coverage. Review of audited split, merge and undo found
camera-match revision and scope races; inactive-clip guards, common locking,
revision updates and nested-undo regressions now cover those paths. Pending/
stale track runs are filtered from play-evidence readiness. Complete replacement
publication also retains the previous tracks, metrics and events when any
previously qualified actor lacks a qualified replacement, including zero-track
runs. Persisted per-clip holds and repeated inherited-label idempotency were
independently inspected. The last completion-state correction and its focused
regressions were then reviewed: new machine clips and held/failed results
remain preliminary, cut readiness is separate, and only actually promoted
unheld results advance the detailed tier. The owner reported 63 focused tests,
formatting, Ruff and type checking passed. A last read found that overlay labels
still included pending/superseded track-bound labels despite current-track
filtering. The reviewed correction restricts labels to selected tracks while
retaining clip-level annotations; 42 focused regressions passed, including held
inherited labels and explicit current/pending/superseded cases. The owner also
reported a fresh empty-database upgrade through 0010, downgrade to base, second
upgrade and no-drift check passed. The final exact backend snapshot passed all
796 tests. The primary owner restarted the actual local API after the label
filter and repeated workspace/media/browser checks, including the full-screen
quiet field. The nested edit/reprocess/undo trace was repeated after the tier
correction; the last label-read predicate does not alter edit behavior.

The work improves real reliability and test coverage. It does not yet meet the
user's full-platform completion criterion.
