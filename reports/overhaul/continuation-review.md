# Overhaul continuation review — 2026-10-21

Scope: follow-up work on `codex/overhaul-completion-20261021`, initially after
PR57 (`7bb3793`), then after the user's `549e8cf` main push. Earlier sections
are historical snapshots; the newest section records the subsequent local,
uncommitted fixes. This is an active review ledger, not full-plan acceptance.
Implementation fixes go to their owners except the explicitly assigned narrow
frontend shot-normalization fix below. Production checks remain read-only.

## Reviewed release hardening

The trusted-main CI trigger, immediate stale-SHA guard, steady serving-SHA plus
database-readiness gate, conservative distinct-image retention and retryable
release-identity polling were inspected. Review reproduced a conflicting
tag/digest history that could delete the only additional rollback. The corrected
planner rejects that contradiction and counts distinct protected tags/digests.
Nine focused Python tests passed independently. Actual previous provider logs
confirm the tag/digest format. These changes have not been deployed by this
reviewer. See `production-recovery-review.md` for actual production recovery,
successful staff-worker heartbeats and unverified capacity/schema/model details.

The live crawl now waits for finite API requests while excluding the intentional
SSE connection. The application actually sets `Accept: text/event-stream`.
HTTP errors, non-aborted network failures, console/page errors and page-content
assertions remain active; this change does not simply ignore failed requests.

## Specialist frontend findings

- Independently running the installed ECharts renderer reproduced invalid
  heatmap cell geometry (`x`/`width` NaN) with a numeric x-axis. The owner changed
  both axes to categories of the exact supplied timestamps and gap subjects.
  A real-renderer/browser regression is required in addition to mocked unit tests.
- Release maps initially skipped explicit loss markers when selecting a nearby
  visible point. The owner added an interval barrier check, preserved negative
  lateral coordinates and accepted the actual `pass_release` event type.
- Event inspection carried a source timestamp, but the selected evidence link
  discarded it. The owner now seeks only the matching source clip using that
  view's own start; alternate recordings retain their independent clocks.
- Missing per-group unknown counts initially became zero. The owner now states
  that unknown coverage is unavailable when the API has not supplied it.

The owner reports two actual Playwright tests passing in 19 seconds and eleven
focused tests passing for rendering and context. The final read confirms the
browser regression inspects actual SVG geometry for invalid coordinates, its
accessible chart label and exact selected-clip seek. The final helper read also
found that `unobserved` was absent from the release-map barrier states and that
the producer's `forward_pass_release` alias was absent from its event lookup.
Both are now corrected with regressions. Missing visibility must never be turned
into a release location. No additional blocker remains in this frontend slice.
The charts consume qualified API evidence; test packets are not football truth.

## Worker findings under owner correction/review

- Film association: automatic validation booleans must not approve counts;
  a nonplay affects its local attempt rather than every rep in a continuous
  shot; proposed links must not rewrite canonical authority. Owner corrections
  and regressions are ready for final verification.
- Persisted grouping: confirmed association metadata must reach actual backend
  canonical `PlayView`/play grouping. Counting an additional view once differs
  from excluding every measurement in that view. Require real API integration.
- Camera safety: coarser motion features must not erase short camera-cut resets,
  and an inferred viewing angle must not automatically prove a fixed camera.
- Position producers: tackle residence requires its own release/sack/exit
  window rather than stopping at scramble initiation; unrelated pending event
  evidence must stay excluded; generic contact does not prove first contact.
  Source/shot metadata, canonical coordinate bounds, unknown shot continuity,
  independently observed entry crossings and paired interaction identity need
  explicit regressions. The owner accepted these findings; output remains
  experimental without actual publication approvals.

The final scoped film-association read confirms that native camera-cut auditing
is separate from the 10 Hz motion features and no longer enforces a minimum shot
duration. Confirmed human alternate views share their real `play_call_id` and
remain eligible for measurements, with a separate false snap-contribution flag.
New angle appearances default to an unknown capture regime. Actual backend
canonical grouping still requires its integration regression; metadata alone
does not prove it. Supplied-footage canaries retained source intervals but found
zero qualified actions/proposals in the sampled 10-second practice and 30-second
game excerpts. This is an explicit remaining semantic-model limitation.

Independent input probes reproduced an out-of-range LOS (`999`) emitting a
`-948 yd` signed gain and an exact-timestamp visible sample winning over a
same-timestamp loss marker. The owner corrected both. Repeating the independent
probes now returns no run measurements for that LOS and no exact/interpolated
position at or beyond the conflicting observation. A simultaneous pressure onset
at release is conservatively censored without zero-length coverage intervals;
independent assertions passed. Earlier fixes for separate
scramble/pressure/corridor windows, pending-event
exclusion, explicit first contact and paired defender identity are visible in
the current implementation. They do not establish real-film football accuracy.
The producer owner reports 1,430 worker tests passing (one skipped, one
integration deselected) in 43.33 seconds on that package snapshot, plus clean
worker Ruff checks and one additive central-definition test. Later association
and API edits require their own final combined snapshot verification.

## Atomic recut architecture

A fully computed machine-cut batch may replace a bare provisional layout in one
transaction. Every usable published real track/event set must instead protect
its existing IDs, regardless of whether specialist analytics qualify. Human
annotations, identity inheritance, manual metrics/events, boundary/history and
canonical associations also protect a layout. In those cases the worker must
receive and reuse the complete authoritative boundary layout, rather than merely
fail a replacement request after expensive inference. Cut readiness may precede
later inference for a genuinely bare layout; a failed or stale batch must leave
every existing active cut untouched. Originals and historical deep links remain
retained. Source-window union coverage, retry idempotency, concurrent corrections
and published-fast-to-detailed reprocessing need explicit integration checks.

The final API read confirms source-window union coverage, deterministic output
IDs, immutable generation manifests, an explicit publication savepoint and the
shared protected-layout response. The owner reports nineteen real PostgreSQL
clip-edit tests on a new disposable database, including injected second-row
failure retaining exactly the old rows, shared canonical camera views counting
once, and published fast tracking activating full-layout overrides. One final
stale-target race was found: new staff mutations to an old clip ID must reject
after that clip is superseded, including a request that waited for the recut
transaction. The owner added post-lock active-target checks for clip, event,
label, metric and correction writes; the final read confirms those guards while
retaining historical idempotent receipts and read-only deep links. Duplicate
caller-supplied IDs are rejected before insertion, and new batch records force a
current state. Worker stale-attempt/body-job guards and the final backend suite
are pending.

The event creator fallback now distinguishes staff work from worker-created
pending automatic events: `created_by` is populated for both, and the fallback
excludes job-associated automatic evidence. The owner separated qualified
canonical play count from active source-window count, preserving context access.
The actual local integration record exercises upload, queue, worker, atomic
publication and playable proxy on an explicitly synthetic source; unavailable
outcome predictions are suppressed and tracks remain quarantined. This does not
establish real-football accuracy or a production release.

## Outcome-model and movement continuation

Initial adversarial P5 fixtures reproduced acceptance of boolean schema/reviewer
metadata and unrelated hash-matching bytes presented as feature/annotation
evidence. The owner now parses typed artifacts and binds exact feature values,
units, lineage and clock, plus exact human target attribution/value, to verified
bytes. Review metadata requires actual strings and a timezone-aware timestamp.
Model load rejects malformed lineage; holdout evaluation binds the full immutable
snapshot set, rather than a favorable subset. Missing game IDs no longer count
as one known game. Checkpoint identity includes model approval state and actual
expected-outcome definitions. Final timing/domain regressions and combined tests
remain pending. Independent repeated fixtures now load the valid twelve-row typed
fixture and reject boolean schema/reviewer/time fields and a changed feature
value despite updated dataset/manifest hashes. No model was trained or registered
by those probes. Predictions remain experimental and unsafe for qualified
publication; no actual football model has been trained or promoted by this review.

The added training flow must distinguish shared local files from worker-local
files behind a hosted API: the hosted API cannot open the laptop's Windows paths.
A new expected-outcome release service is now under review. Review found that
recomputing losses from submitted predictions did not bind those predictions to
the actual checkpoint. The owner added a separately queued worker verifier that
reloads the frozen artifact and recomputes feature-only forward outputs. The API
requires the configured processor principal, owned runtime, current live lease
and attempt before recording completion, then rechecks exact challenge/job/model,
receipt, dataset and artifact bindings at evaluation and human promotion. Ordinary
staff success callbacks and a raw audit cannot satisfy this boundary.

The verifier review identified two further source-integrity gaps: independent
group declarations could reuse the same actual recording/media, and a list named
`verified_media_sha256` did not prove the independent worker reopened the source
bytes. The final implementation now checks disjoint typed memberships, reopens
and hashes actual media, verifies native clocks and recomputes frozen model
outputs in a separately claimed verifier. Small fixtures must fail the 240 canonical /
12 independent group, balanced-category and calibration release gates. No real
expected-outcome model has passed or been promoted.

Movement review reproduced loss of one actor when multiple participation events
were reconciled as scalar events. The owner fixed distinct-ID observation
reconciliation and end-only participation clock normalization with actual accept-
path regressions. The prior movement snapshot passed 1,509 worker tests (one skip,
one integration deselected) and its cross-service test. The final movement helper
read confirms separate special-teams temporal event/ball-flight evidence from the
kicker's later visibility and spatial mapping, duplicate-ID rejection and refusal
of explicitly unverified nominal-FPS timelines. A legacy explicit-player-ID compatibility rule
is retained at root's direction; absent approval metadata does not itself prove
new identity qualification. Backend eligibility remains authoritative.

Review found a full-workflow binding gap: saved actor/center/opponent/ball track
UUIDs differed from new job-scoped track UUIDs. The implemented server binding
now captures the complete native observation fingerprint under the exact full
source, clip and window. Worker replay requires a unique one-to-one match before
role hydration and rewrites explicit single/list/map actor references with
lineage. Changed, missing, duplicate or malformed observations request review.
Actual manual label proof is checked again by the API before identity writeback;
a later staff reassignment or unassignment overrides inherited provenance.
Real PostgreSQL correction-to-fresh-track/worker-producer tests exercise this
path. Shot IDs are normalized across integer/string transport, preserving zero.

The new opportunity API deduplicates actual canonical play/player/kind/definition
observations independently of successful measurements. A qualification-only
change initially omitted dataset-version invalidation; the owner now includes
qualification and clip eligibility in the fingerprint with a regression. Root
retains resolved-identity cohort aggregation: anonymous team geometry remains in
film overlays, and anonymous cohort products remain an explicit limitation.
Movement frontend guards preserve source clocks, actor bindings, line breaks and
signed closing speed. The API and frontend now require matching source-event
proof for ground-landing locations. Partial measured geometry uses products with
a null scalar, preserving observed prefixes without complete scalar summaries or
invented duration censoring. Final combined continuation checks remain necessary.

The next measured-geometry slice is being reviewed: explicit polygon envelope
dimensions/area/compression, original OL geometry, signed receiver depth and
source-linked receiver point events, plus further participation denominators.
Early review found repeated new event types missing from reconciliation, an
explicit center ID able to reference a non-center, unknown shot sentinels accepted
by truthiness, and a frontend envelope dedup key missing its subject. Owners
corrected these with regressions. Roster cohort aliases consistently select
OL/DL/DB subpositions while retaining explicit coaching groups and never
inferring observed on-field roles.

At root's narrow implementation request, this reviewer added the shared frontend
`canonicalShotId` normalizer and used it in geometry, movement, interaction and
qualified-position continuity. Four new component regressions first reproduced
unknown-shot acceptance and numeric-zero/string-ID mismatch; then all 27 tests
across five files passed. Global TypeScript and scoped ESLint checks passed.
Unknown samples remain line barriers and cannot create geometry or interaction
evidence. Root independently accepted this small reviewer-owned code change.

The final frontend packet read matches real measured-envelope, rusher-distance
and explicit interaction producer shapes. The envelope key now includes its
subject; invalid samples stay barriers, and rusher curves require the actual QB
track binding. The interaction matrix requires explicit matching endpoints and
the same known source shot. Its producer must also enforce that shot constraint
so scalar summaries do not disagree with the matrix. Root reports four actual
chart-browser regressions, including polygon sample-to-film seek and interaction
matrix-to-film seek. Broad roster cohorts now have real-query alias tests and
preserve explicit coaching groups separately from observed on-field roles.

P5 serving eligibility now binds prediction lineage to the actual server
recording/full source hash, canonical PlayView, clip clock and task subject.
Completion, rushing and YAC persist the actual QB/rusher/receiver track; contextual
pressure is explicitly team scoped. Feature schema v2 requires reviewed original
OL and defensive roles, population completeness and actual known call context;
it no longer names geometric guesses as LB or blitz evidence. These guards do
not supply the missing reviewed football context or trained models.

## Full-source proof and verifier transport

The final source-identity read found no blocking trust-boundary defect. The worker
computes the full SHA and the API-compatible sampled registration fingerprint
from actual bytes, checks source stat stability and synchronously submits the
receipt before dependent stages. The API requires the configured processor
principal, its owned runtime and the current matching unexpired job lease. Its
immutable server-owned record survives later analysis failure/retry and cannot
be populated through video metadata or ordinary settings. It establishes source
identity only, with no metric/model approval. The final source stat is checked
before temporary-source cleanup.

`expected-outcomes-http-verification.json` records the actual local HTTP training
queue, runtime registration, claimed verifier, exact-weight/native-media forward
verification and release evaluation at 18:37:41 UTC. The small explicitly
synthetic protocol candidate failed the 240-play/source-group/adjudication gates
and promotion returned 422. This closes the transport implementation gap without
qualifying a football model. A subsequent source/binding/initial-outcome focused
rerun passed 36 tests in 0.73 seconds; these are distinct snapshots from the full
suite counts. Root reports 456 frontend tests, 19 browser checks, build and global
ESLint passing before later outcome additions.

The reviewer also reran fourteen backend source-proof unit cases in 0.19 seconds
with fake database sessions and an inert localhost configuration; no database
connection or production credential was used. They cover foreign principal,
runtime, lease/attempt/expiry, source registration and metadata-forgery refusal.

## Reviewed outcome slice and structured feature context

Independent probes reproduced three false positives in the new reviewed-skill
producer: conflicting complete/incomplete duplicate results were dropped and
then treated as absent, allowing YAC; a carrier cut after its explicit terminal
event remained eligible; and an earlier reception in another shot established
cut possession. Further probes reproduced a cut after an explicit incomplete
result and a duplicated event ID with changed time counting twice. The owner
corrected all five cases: conflicts poison their possession, explicit result
contradictions block cut starts, known same-shot qualifying possession precedes
the cut and no earlier terminal is permitted. Nineteen worker cases and an
actual correction-save -> human-event GET -> independent producer test pass on
that slice. These fixtures remain mechanics, not football labels.

The reviewer then implemented the explicitly assigned optional
`PredictionContextFields` intake with parent review. It authors eligible
snap/handoff/release/catch context, named complete defenders, original OL and
optional TE roles, observed defensive roles/gaps, actual known calls and measured
down/distance/LOS/direction/ball position. Missing values and unknown populations
block context submission; none become zero. Availability is converted from play
seconds to recording seconds once. Context-enabled events use recording time and
an actual exact same-shot native source frame shared by every required track;
playback between frames can select a supplied source frame without FPS arithmetic.
Ordinary observations retain their prior clip clock. No future target label is
included. Parent review caught valid optional LTE/RTE being refused; the complete
observed map is now retained while pressure spacing uses the original five OL.

Four mounted POST paths share an explicit numeric protocol fixture with the real
worker extractor. Thirty-four focused UI cases pass, including missing/future/
contradictory context, zero shot/availability and optional TE handling; global
TypeScript and scoped ESLint pass. Fifty-eight intake/expected worker cases pass
on that scoped snapshot. Root ran the actual PostgreSQL correction POST -> GET
event -> independent worker extractor test: a nonzero ten-second source offset,
recording timestamp 12.5 and actual native frame 375 reach all four extractors.
No actual football features, targets or model approval were created by the tests.

The sidecar/repair notice final read also accepts current-job/runtime/lease and
revision checks, effective human-event filtering, bounded diagnostic fields,
clip-keyed generation guards, immutable editor revisions and PATCH/DELETE CAS.
Malformed actor references retain their reason without aborting diagnostic
publication. Exact matching still abstains when observation geometry changes.

The final lane-window read found conflicting handoff actor/carrier aliases still
producing aligned widths. The owner now requires one coherent carrier identity;
the corresponding regression passes. Discrete source snapshots bound transitions
without claiming continuous openness or a successful running path. The reviewer
ran 115 intake/expected/skill/lane/actor cases together in 3.61 seconds, all passing.
Subsequent final findings were corrected: explicit nested context clock tags
must be recording time and are hash-bound; distinct human clock IDs survive
reconciliation; a rejected staff clock leaves an inert review marker so the
predictor cannot silently fall back to the disputed automatic clock.

## Connected feature export

The worker now publishes actual normal-stage P5 feature snapshots as a bounded
content-addressed artifact, with exact typed evidence and source/clip review
revisions. The API requires this completed job's own trusted processor source
attestation; a different job cannot borrow a recording's global hash. It checks
the actual Video/session/canonical/clock, active clip and unchanged annotation/
observation revisions. The signed file endpoint repeats those checks at fetch,
returns the exact hash-checked canonical JSON bytes with the advertised attachment
filename and uses private/no-store caching. Local and S3 reads remain bounded;
there is no backend inference.

Learning exposes the actual source job/recording, signed download, checksum and
existing blank-target/prepare commands. Target values, review attribution and
splits stay unfilled until staff supplies them. The final source read found no
remaining blocker in this export boundary. Seven export/training UI cases pass
independently in 3.20s. A targeted worker run passed 81 cases and exposed one
stale exact-dictionary assertion after a parallel `removed_events` checkpoint
identity addition; the owner was asked to update that expectation and verify
tombstone invalidation, preserving the new field.

The root then identified a separate production publication issue: ordinary
analyst metric POSTs could borrow approved model metadata without a processor
job/lease. Review independently confirmed it. The implemented dependency now
requires the configured processor principal, its owned runtime, current running
pipeline lease/worker, nonmissing future expiry and no cancellation. The router
binds the body job/video/optional clip for every qualifying real automatic metric,
stamps a server producer audit and keeps results pending until completion. Copied
lease fields and mixed-case prediction names cannot bypass it. Idempotent prior
receipts remain read-only. The sixteen-case actual HTTP matrix covers these
denials and a trusted pending-to-completion positive; root reports 36 focused
backend cases passing. The independent source read found no remaining blocker
in this production-writer boundary. No actual production model or football
result was created by these protocol tests.

Missing staff annotations, held-out evaluations and trained model evidence remain
unavailable. New geometry helpers or successful synthetic regression cases do not
qualify them or complete the remaining twenty-page implementation requirements.

## Original-five display and newly supplied drone folder

The original-front component consumes actual specialist producer packet shapes
for the original LT/LG/C/RG/RT, preserving clip/video/canonical/result/definition/
snap/shot separation. It displays signed observed displacement with loss/gap
barriers, exact source-frame geometry and film seeking; missing actors stay
unobserved. Independent review found two native-clock defects: one native frame
could map to two source timestamps across actors, and out-of-order actor samples
were retained after a gap marker. The owner added bidirectional frame/PTS checks
and refuses nonmonotonic paths across barriers. All 16 component plus 9 existing
TeamAnalytics tests pass independently in 4.47s. The fixture is explicitly a
synthetic producer contract, not accepted football evidence.

The newly supplied `OneDrive - University of Toledo/Drone Footage` folder was
inventoried and every original file fully read, hashed and CPU-decoded without
editing or moving media or controlling the live processor. At 19:26:16 UTC the
audit counted 30 MP4 files, 300,495,546 bytes and 11,065 frames, all 1280x720 with
30000/1001 rates and actual strictly increasing PTS. The longest source is
23.623600s; total 369.202164s is distributed across 30 clips. Full-hash comparison
then showed all 30 duplicate existing verified catalog bytes, no new independent
source population. No OneDrive placeholder failure occurred and source stats
remained unchanged. This folder cannot satisfy a continuous >=300s 1080p timing
gate. Machine-readable paths, hashes, metadata, PTS/decode checks and catalog
matches are retained in `drone-original-inventory.json`; the companion Markdown
report states the limit explicitly.

## Reviewed removals and legacy classifier gate boundary

Revisioned event deletion now retains the original Event and a server-owned
CoachCorrection tombstone. The worker uses the actual source/clip/window, exact
known native clock/shot and unique observation binding before suppressing an
identical model proposal prior to persistence. Independent review caught a
same-event-ID shortcut that could suppress changed actor evidence; it was
removed. Both reused and new UUIDs now require exact normalized content and
successful binding. Changed/missing/ambiguous evidence leaves current proposals
and a source-review warning; independent human events are preserved. The
reviewer reran removal, actor-binding and orchestrator tests: **60 passed in
16.74s**, including tombstone cache invalidation. The owner separately reports
an actual staff API soft-removal/history to independent worker dispatch test.
No source blocker remains in that scoped boundary.

A separate pre-existing classification release limitation remains: in
`backend/app/services/learning.py`, `baseline_gate` accepts absent
`baseline_accuracy`; generic promotion rechecks a training snapshot only when
a snapshot hash was recorded. Thus a legacy recorded classification pass does
not establish a measured improvement against a training-population baseline
or the full twenty-page specialist gates. New expected-outcome releases have
separate stricter baseline/population/verification requirements. The reliability
UI must distinguish recorded legacy classification gates and unavailable
baselines from full-plan acceptance; enforcing a mandatory real training
baseline for future generic classification promotion remains implementation
work, not merely missing football test data. No historical record was changed.

## Reliability diagnostics and future baseline closure

The final diagnostics read accepts stored/candidate/current/approved separation,
missing historical data, exact actual bin/matrix totals and authenticated
reload/download. Independent review found two defects: contradictory sample/
canonical/category populations could display a passing recorded gate, and
float tolerance allowed fractional diagnostic counts. The owner added exact
integer-not-boolean schema/count/threshold checks, population reconciliation
and safe-integer prediction coverage while retaining tolerance for measured
floating statistics. Independent checks: five backend diagnostic/parity cases
passed in 1.37s; 33 mounted UI cases passed in 5.54s (7 reliability, 16 original
front, 10 repair). The parent reports the actual browser protocol-fixture
calibration/authenticated-reload/download/filter test passing; routes are
mocked, and this is not a real heldout football evaluation.

The separately assigned generic-classification baseline fix now requires a
hash-bound frozen human training snapshot of the same label type, rejects
frozen clip overlap, and derives the actual training-majority class and
heldout baseline. Future promotion recomputes saved heldout scores/confusion
and the immutable baseline, requiring strict improvement and existing score
thresholds. Missing old policy or forged baseline returns 422; historical data
and existing production bindings remain unchanged. Owner reports 44 focused
backend cases. That closes the baseline-optional code gap described above.

A different pre-existing classifier trust boundary is still open:
`routers/labels.py:create_label` accepts ordinary staff model labels with
caller-declared real provenance and a model-version ID; generic classification
evaluation trusts those persisted fields. Baseline statistics alone do not
prove those labels were outputs of the actual artifact. The parent was notified
to bind these generic predictions to authorized trusted execution before
claiming classifier release integrity. P5 has a distinct frozen challenge and
actual-weight worker-verification path; it cannot be assumed for generic labels.

## Browser navigation infrastructure review

The full browser run exposed a Back-navigation failure while Next development
routes were compiling. Owner traces place a Fast Refresh full-document
clip-review GET before the actual Back click; the correct team-stats navigation
was then aborted by that older document load. Independent source review found
only explicit user navigation handlers, with no auto effect pushing the old
clip. The final test-only correction awaits real HTTP bodies for team-stats and
clip-review sequentially in Playwright global setup before any concurrent
browser. The test additionally asserts the saved-view URL has committed.
Actual Back/context/error assertions remain; no sleep, retry, timeout expansion
or product navigation workaround was added. Owner reports 15/15 warm repeats
and 5/5 fully cold analytics cases passing after setup. This addresses the
Next-dev test infrastructure condition, not a claim about production deployment.
The reviewer accepted the three-file change by source/trace read.

The parent subsequently reports both actual local transport integration cases
passing together in 46.09s after fixture isolation was corrected. The reviewed
fixture now performs normal runtime registration before claiming a pipeline
job, binds each actual server/store through reversible environment/module
patches, and invalidates shared authentication/registry/router caches before
and after. It does not bypass protected producer checks or synthetic output
quarantine. The initial two-route browser precompile pass is not a complete
browser acceptance result: a later full run still exposed cold login-route
HMR failures, and the browser owner is extending deterministic startup before
final full-suite acceptance.

## Time-to-throw clock review closure

The parent identified an independent TTT bypass after P5 clocks were hardened:
a metric could match any manual snap/release pair, and unknown clock metadata
could be interpreted as recording time. The correction requires one effective
reviewed snap and one forward release with explicit recognized source clocks,
a single known same shot, clip-bounded times, a strictly positive duration and
packet source bounds matching those events. Worker selection preserves distinct
reviewed IDs, prefers the unique reviewed clock over automatic aliases and
respects inert disputed-clock markers. Unqualified candidates remain explicitly
experimental/review-required.

Independent source/probe review found four additional false qualifications:
missing clock tags defaulted to clip time; an extra unresolved manual snap was
dropped before counting; conflicting attribute/provenance shot IDs were ignored;
and zero duration was accepted. All four were reproduced through the actual API
qualification helper using an inert fake database. The parent fixed each case
with real PostgreSQL regressions. On the final source the reviewer repeated the
same helper probe: all four were refused while an explicitly mixed clip/recording
clock with nonzero offset remained accepted. Eighteen standalone worker TTT cases
passed independently in 0.37s. Parent reports 30 scoped backend cases (13 TTT,
16 producer-write and one legacy positive) passing. No remaining blocker was
found in this reviewed clock scope; these are protocol checks, not football
benchmark labels or a release approval.

Label coach-read review also corrected actual publisher contract mismatches:
the selected clip field is `published_result_version` and publication holds are
a dictionary. Current approved output now requires that selected completed
private proof; held/stale jobs and unapproved candidates remain hidden, while
human corrections take precedence, identical copies deduplicate and conflicting
human meanings require review. The actual HTTP proof-to-publisher-to-overlay
positive/held/replacement/history regression was read independently. Fourteen
qualification/selection unit cases passed independently in 0.20s; all twenty
ClipReview UI cases passed in 8.11s. Producer proof and training retry closure
remain the expected-outcome owner's separate active review.

## Generic label trust, capture and staff-caption final review

The previously open caller-declared classification gap is corrected in the
current continuation. Real generic model labels require the configured processor
principal, owned runtime and live authorized lease. Private immutable proof binds
the actual artifact bytes/schema, label body, exact source/clip/review identity
and attempt; candidate training also binds the actual frozen holdout row and
trusted source job. Server completion freezes the final publication output hash
after holds are inserted. Evaluation, new promotion and coach reads recheck that
proof and successful current attempt. New execution-scoped model/label IDs and
preserved training-dataset scope allow legitimate retries without upgrading old
failed history. No backend model inference was introduced.

New generic classification promotion recomputes the frozen training-majority
baseline and actual heldout scores, including per-known-capture slices. Unknown,
undersized or failed slices prevent approval. The centralized approval builder
binds exact artifact/evaluation/task/schema/capture support. Coverage source proof
additionally requires its actual matching safe calibration, confidence/threshold
at least 0.7, finite invertible homography and no disqualifying reasons; current
source or calibration changes revoke visibility. The reviewer read the real
proof-to-publisher-to-overlay and safe-calibration revocation paths and independently
reran 22 coach-visibility units (0.32s). Owner reports 97 focused backend cases and
27.57s queued failed-first/public-retry/actual CPU fitting/HTTP evaluation and
promotion. Those are numerical protocol fixtures, not real classifier accuracy
or the full plan's benchmark population.

Both rendered preview paths now accept only a separate authenticated staff-label
snapshot, with actual annotator, clip scope and unambiguous effective values.
Raw Clip.label_data and public model flags cannot supply STAFF captions or be
laundered into source-human downstream label evidence. In-flight automatic
numeric callouts are omitted. Human-label snapshot identity and the v5 generator
invalidate old cached output. The reviewer independently ran 79 renderer,
snapshot, orchestrator and recovery cases (21.30s), including real encoder paths
and metadata-versus-authenticated-label regressions.

Final review found two narrow defects after that pass: label pagination lacked
an ID tie-breaker, and label deletion did not advance the annotation revision,
so a concurrent staff-label removal could escape snapshot CAS; separately,
unavailable/disabled/unverified-adapter sources were not rejected by the new
caption helper. These were sent to the owners with an actual helper reproduction.
Final corrections and the complete frozen backend/worker/three-transport gates
remain pending verification at this entry. The parent reports the final frontend
541-unit/22-browser/build/lint snapshot passing; its browser warmup now prepares
all actual suite routes, preserving authentication and navigation assertions.
No production restart, deployment or database mutation was performed by this
reviewer, and the current continuation is not full twenty-page acceptance.

The final caption/source consistency findings are now corrected. Independent
positive/negative probes retain a legitimate staff annotation and refuse all
14 invalid-source value/provenance combinations; 27 renderer cases passed in
3.07s. Label lists have a stable timestamp/UUID order. Human/manual removal now
clears the effective cached tag, advances the annotation revision, resets review,
invalidates dependent metrics and preserves a non-training deletion correction.
The locked reread explicitly refreshes the already-loaded SQLAlchemy identity,
covering a concurrent model-to-human correction while waiting for the clip lock.
The owner reproduced that cached-object fault and reports five actual API/DB
deletion/order/identity-map cases passing in 1.10s. Final source/test read found
no remaining blocker in the reviewed generic-label, coach-read or caption scope.
Full frozen backend/worker and all three transport integration cases remain
separate final gates; the historical counts above must not be combined.

The parent then found an actual CI portability defect in the new generic HTTP
fixture: its seed guard required literal port 55432, while CI uses loopback
5432/footiq_test. The corrected guard parses the URL and accepts only explicit
PostgreSQL schemes, exact loopback hosts and two exact disposable database names,
independent of valid/default port. Query/fragment destination overrides, remote
hosts, multihost URLs and unlisted databases remain refused; errors never echo
credential-bearing URLs. Independent pure tests passed 20 cases in 0.10s without
database or network access. This acceptance covers the generic seeding guard;
the existing shared fixture still requires its explicitly configured test URL.

## Actual game-window failure review

The later real MIST 300–320s source-window run is retained as failed/partial,
not successful full-game acceptance. Its 1,178 decoded pictures match the parent
in order through explicit per-frame MD5/native-tick evidence; derived clocks can
differ by one tick and are not described as unchanged parent timestamps. Current
parent hashes reuse the earlier verified catalog, not a new 3.74GB full read.
The run used normal isolated registration/claim/source proof and actual cached
CUDA detection, generated five unresolved windows and withheld all coach facts.
Four clean signed proxies succeeded, while a real positive one-frame window
rounded to zero proxy frames. The reviewer independently reproduced nonempty
native source indices (2,3) with zero proxy samples at 30fps. The 146s job failure
remains a concrete application defect pending the parent's fix and rerun.

The first mapping audit compared six-decimal FFprobe timestamp representations,
not exact native integer ticks. Independent review caught that precision claim;
the owner renamed the check, records integer_tick_precision_preserved=false and
maximum 3.33333336e-7s error, and retains exact source ticks. Duplicate decoded
parent best-effort timestamps were initially described too broadly as original
presentation ticks. A subsequent full demux audit found 301,950 original packets,
zero missing/duplicate integer packet PTS and strict sorted presentation times.
The manifest now distinguishes decoded best-effort timestamps from actual frame
PTS/DTS for all 1,178 selected pictures (37 differ); picture equality is retained.
Thus the earlier decoded duplicate does not prove a duplicate packet timeline or
nominal-FPS fallback. The reviewer read that demux evidence; no full-game pipeline
run has been performed. The inventory establishes long 720p sources are available;
execution/performance/accuracy and compatible production acceptance remain gates.

## Native-clock and positive-window correction closure

Final review accepted integer packet/frame PTS plus actual stream timebase and
duration ticks, with source origin subtracted before float conversion. Actual
frame PTS take precedence over decoder best-effort estimates; rounded decimal
fields remain compatibility fallback only when exact fields are unavailable.
The reviewer independently passed all 25 PTS cases in 5.23s and read the first
original MIST packet-probe evidence: it returned verified with 301,950 strict
timestamps and no nominal-FPS fallback. A later decoder-origin check exposed
discarded preroll in that index, as documented below; this earlier return was
not proof of displayed-picture alignment.

The reviewer also reproduced an adjacent-frame bug: quarter-FPS decoder lookup
mistook the first of two actual timestamps 1/60000s apart for the second frame.
The final class/readers now use actual nearest native timestamps, strict playback
floor and half-open source ends, and discard late/duplicate decoded pictures
instead of advancing a fabricated frame counter. Stall bounds and cancellation
checks cover iterative, seeking and selected-frame reads. A positive nonempty
source interval retains at least one CFR picture without changing its semantic
source bounds; an empty interval still yields no invented picture. Measured
duration tolerance remains one output frame. Clean proxy v3, overlay v6 and
timeline revision 3 invalidate older derivatives/checkpoints.

The independent final combined targeted run passed **91 cases in 19.25s** across
PTS, frame readers, short-window proxies, actual encoding, render windows and
recovery. No remaining blocker was found in this scoped derived-window fix.
The parent separately reports the backend frozen snapshot at 1,024 passed,
122-file static typing and 234-file formatting clean, and the unchanged frontend
541/22-browser/build/lint pass. Whole worker, all-three actual transports and the
real fixed game rerun remain separate pending final gates at this entry.

The original-only display-origin audit then found the first two negative packets
were explicitly marked discard/preroll (`D`), while stream start and the first
decoded picture were at zero. Including them had shifted the packet timeline
origin by 2002/60000s. The final narrow probe correction requests packet flags
and excludes only explicitly discarded packets before PTS validation and origin
selection. Visible negative PTS and old records without flags remain intact;
decoder input packets are unchanged and no nominal-FPS repair is used. The
reviewer independently passed **50 combined PTS/native-proxy/frame-reader cases
in 10.60s**, including missing-PTS discard handling and negative-origin retention.
Source read found no remaining blocker in that correction. The owner then ran a
fresh actual original probe: 301,948 non-discard timestamps, origin zero,
verified=true and no nominal-FPS fallback. The first five actual OpenCV picture
timestamps resolve to native indices 0,1,2,3,4. The reviewer read the retained
pre/post-filter and decoder-origin evidence. This closes demonstrated first-five
alignment only; later original decoder recovery, complete-game execution and
the bounded fixed game rerun remain separate acceptance work.

The first combined three-transport run passed two cases but exposed another
fixture defect: selecting MAX(priority) across completed history overflowed
after a prior generic fixture used INTMAX. The reviewed test-only correction
examines only other queued jobs, refuses a competing queued INTMAX before any
write, and parameterizes an UPDATE targeting only the newly created job UUID.
Historical/unrelated rows remain untouched; normal registration/claim and
publication gates are unchanged. The parent's combined rerun remains pending.

The final combined run subsequently passed **all three actual HTTP transports
in 57.58s**, with the 20 pure scratch-guard cases deselected. The reviewer read
`runs/continuation-all-three-integrations-final.log` and the retained test paths:
normal API/runtime/queue/atomic cuts/clean proxy and synthetic quarantine; actual
P5 queued checkpoint/holdout reload with small-population promotion refused; and
actual generic CPU fit/failed-first retry/private producer proof/evaluation and
promotion on numerical fixtures. Older detailed JSON IDs/timestamps were not
regenerated by this latest run and remain historical evidence. This combined
protocol pass does not qualify a football model or a production rollout. The
fixed actual game-window rerun and final whole-worker gate remain pending.

## Final frozen local continuation review

The reviewer read the final worker log: **1,764 passed, one optional joblib
custom-regime artifact test skipped, three integration cases deselected,
61.72s**. The actual three HTTP transports passed separately in 57.58s. Parent
reports 1,024 full backend cases, 122-file typing and 234-file formatting clean,
plus 44 affected backend-to-worker/proof/visibility cases after the clock fix.
The unchanged frontend snapshot is 541 unit cases, 22 browser cases, build and
lint passing. No totals from these different runs are added together.

The same-source game rerun `d6c455ec-8d26-48b3-925d-c40d1acc9b51` succeeded in
140.938s. Independent evidence read confirms the private completed-job/source
receipt binds its actual full derived SHA, job, attempt and registered runtime.
Five authenticated signed H.264 proxies have 492/1/1/88/19 frames, and all 601
mappings match the exact derived integer tick/60000 float representation with
zero measured error. The one-tick and 1001-tick source windows both retain a
picture, without changing their semantic duration claims. The earlier failed
attempt remains separately retained. CPU tests ran in parallel, so this observed
wall time is not a controlled speed benchmark.

The reviewer inspected both retained preview pictures. They show the calibration
warning and anonymous boxes, with no added football HUD. The scoreboard/poster
picture includes detector boxes on poster images, an honestly documented quality
limitation. The successful engineering run has 161 anonymous fragments and 38
withheld raw heuristic labels; qualified events, labels, metrics and identities
remain zero. Unsafe calibration/unresolved plays still abstain. No all-frame
OCR, full-game processing, 1080p/p95/overnight, football-quality or production
acceptance is inferred. The owned API stopped, GPU returned idle, and the
existing long-lived worker remained untouched according to retained metadata.

All 209 files in the parent's frozen source manifest independently matched
current bytes. Compact sorted-key JSON hashing of the manifest's file list
reproduced aggregate source SHA-256
`9c76d4d7d8f634a8c69508367d5c14fecede36157874c680f66b943040416f43`.
Base is `7bb3793`; the continuation remains local/uncommitted/undeployed. The
reviewer independently verified the fresh staff archive's 69-member CRC/path
checks and SHA-256
`a11f3a0411edb84f93a88b6a2d75ea98965ec468e08e8fdb180e07e26ca2f771`.
The reviewer also verified all 67 internal byte sizes/hashes, with no mismatch.
It contains metadata/tools/
docs and unreviewed templates, without films, model weights or completed truth.

No remaining blocker was found in this reviewed local source and bounded
engineering scope. The twenty-page platform remains incomplete: specialist/
conditioned-product work, real staff benchmark and task models, sustained/full
original game acceptance and a compatible deployed staff trace remain separate
obligations, as detailed in the page-by-page ledger.

The final master Markdown and machine-readable record
`continuation-verification-20261021` were read independently. All eight recorded
test-log hashes and the current game-report hash match the actual files. The
record accurately separates local regression/engineering passes from remaining
implementation, real-label/model/performance and production obligations. The
only requested documentation correction was removing the assertion that actual
command strings are recorded in the JSON, which records check names, results,
paths and hashes instead. No additional source or bounded-evidence blocker was
found in this final review.
The parent removed that command-string claim; the corrected master wording was
confirmed and the final review documents pass `git diff --check`.

## Review after the user's 549e8cf push

The reviewer independently refreshed GitHub run `39676607970` at 21:35:08 UTC:
worker unit and cross-service integration jobs failed; seven other jobs
succeeded. Its source is `549e8cf30ced3f4ed6f914f2a21263a0baf226d3`. The new
local passes below do not change that failed remote run. Read-only production
evidence at 21:23:46 UTC proves an actual mixed release: canonical Vercel
frontend `549e8cf` (`dpl_EkjHxmNNKzQDkw6hjLJt9CauKHxu`), API `7bb3793`, health
and database readiness 200, protected inventory 401. The long-lived processor
remains unchanged, with the inspected separate checkout at `d1f4317`.

### Linux source identity

Review traced both reported CI failures to inode ctime changing when stages
borrow/release hard links. The corrected source check preserves dev/inode/size/
mtime guards and rehashes actual full bytes on ctime-only change, requiring
the original full SHA and registration fingerprint and stable before/after
stat. It does not refresh proof to changed bytes or add a redundant hash for
unchanged metadata. Independent media-identity/orchestrator rerun passed 31
cases in 16.81s. The parent additionally reports the actual Linux-container
failure case passing and three actual local HTTP transports passing in 60.46s.
This addresses the reproduced portability failure locally; remote CI remains red.

### Paired trench replay

Independent adversarial review reproduced an interpolated actor position being
advertised as an exact observed sample. The worker now emits an absent-actor
barrier for explicit interpolation/prediction. Distinct same-pair start events
formerly produced duplicate duration rows; unique qualified endpoints are now
required. Conflicting supplied `blocker_id`/`player_track_id` aliases also
formerly chose one actor. The corrected resolver rejects them, including an
extra ambiguous endpoint beside otherwise valid endpoints, rather than using a
fallback. Exact native frame/time, safe calibration, source/shot/pair/result
binding and no extrapolation are required for spatial replay. Missing geometry
does not erase an otherwise valid temporal observation. The frontend preserves
barriers, discrete source samples and exact film focus, without success grades.

### RB run/contact/gap views

Review reproduced contradictory carrier aliases choosing one athlete, native
sample claims without verified frame/time support, an unreachable no-endpoint
path prefix, and conflicting first-contact rows choosing an arbitrary scalar
endpoint. Those cases now abstain or remain explicit partial composite evidence.
Unknown/cross-shot/multiple first contacts cannot produce contact marks or
pre/post-contact scalars. Density counts distinct canonical plays per observed
two-yard cell, omits predicted/interpolated points and does not imply exposure
rates. Frame/clock conflicts, losses and unknown shots break paths. Original gap
labels must match actual bounds; an inward TE no longer makes an inside-tackle
point an edge. Both attack directions use the actual outermost supplied side
tackle/TE center. No intended gap, clear opening or blocking assignment is inferred.

The final independent combined position/trench run passed 72 cases in 1.71s;
RB/trench/matrix frontend run passed 18 cases in 2.97s. These accepted producer
and UI fixtures remain synthetic protocol evidence. The owner/parent's real
browser checks are additional UI mechanics, not real-football validation.

### Heldout source-case/category diagnostics

Review found that a self-consistent supplied case hash plus tolerant numeric
comparison could accept a small alteration to an immutable case. The corrected
server requires exact independently reconstructed case SHA/document, including
sub-tolerance prediction/baseline changes. Four actual CPU-fitted candidate
reports reproduce server case bytes for completion, pressure, rushing and YAC.
Category comparisons preserve the artifact/training-population baseline without
fitting the selected outcomes. Category/source counts deduplicate canonical
plays; confirmed film categories require matching recorded source attestations.

Authenticated bounded pages/full-filter exports bind evaluation/model/artifact/
case identities. The UI reconciles count/category/page identities, marks small
slices descriptive, shows outcome units and rejects stale responses. Raw
candidate reports cannot request a non-UUID stored-evaluation endpoint.
Category/model/evaluation and unfiltered pagination context restore on return
from film. Exact current film links reuse the trusted normal feature export:
source time 2 with clip start 1 resolves to focus 1, while changed review
revision, media identity or canonical association removes the link. Historical
numeric evidence remains readable without claiming a current film match.

Independent backend checks passed 12 cases in 15.85s, including actual four-head
CPU-fit parity, with one actual database API case deliberately deselected.
Independent frontend case/reliability checks passed 16 cases in 5.00s. The owner
separately passed the actual database API/filter/download and current-export
link/revocation cases. Generic-classifier case reconstruction remains missing;
old reports are not reconstructed from aggregate scores. No actual football
candidate or benchmark is qualified by these numerical fixtures.

### Processor compatibility and remaining gates

Read-only source review found that the old running worker advertises `train`
but implements only `local_coverage`; existing job-type/profile claim filters
can let it consume new outcome-training tasks. Legacy pipeline completion can
also succeed without granting any current source proof, so heartbeat and job
success cannot substitute for a compatible release. The corrected local claim
guard requires explicit producer protocol/task support for the two new training
variants, handles registered-runtime fallback and rejects boolean protocol
values. Trusted verifier principal/lease boundaries remain authoritative.
Cached source/dirty metadata reaches registration, heartbeat, claims and staff
inventory. The reviewer independently passed 21 worker capability/dispatch cases
in 1.07s and read the actual database compatibility/principal regressions.
Broad current verification remains pending; no live processor update occurred.
See `worker-release-compatibility-review.md` for evidence and a concrete future
coordinated update handoff. This reviewer has made no production writes,
restarts, credential reuse or deployment changes.

The new scope remains local/uncommitted after `549e8cf`. It closes these
particular implementation gaps, while generic-classifier cases, conditioned
coaching products, route/trench/defense/ST specialists, staff benchmark labels,
actual model fitting/quality, full original-game and speed/overnight evidence,
and a compatible deployed staff journey remain separate work.

The owner subsequently found a weights-only artifact alias in the P5 training
adapter. Final review confirms report/evidence object keys now use their own
full SHA values, candidate UUID/version bind the exact three artifact hashes
with task scope, and equal binding retries remain idempotent. Equal weights
with different reports/evidence retain historical bytes and distinct registry
bindings. Independent new-plus-existing artifact/training tests passed 58
cases in 11.16s. No coefficients, features, split policy or promotion thresholds
changed. Broad final source-bound checks remain pending after this delta.

The final read-only public refresh at 21:48:20 UTC still found canonical
frontend `549e8cf` and API `7bb3793`, with httpx health/readiness/version 200,
database okay and protected inventory 401. An initial urllib attempt received
Cloudflare error 1010 (`browser_signature_banned`); both attempts are retained
in `runs/readonly-cross-release-549e8cf-final.json`. That client-specific denial
is not inferred to be an application outage. No authenticated writes occurred.

The last UI context read reproduced an additional Back edge: selecting a
nondefault model/evaluation without changing All-cases/page-zero filters did
not persist that selection into the film return URL. The corrected successful
load writes the actual model/evaluation plus untouched filter/offset before
capturing the return path. The reviewer independently passed all 17 case/
reliability UI tests in 5.80s and read the new real-browser regression selecting
both nondefault controls, opening film and checking the restored controls,
cohort and zero offset. Final broad browser/build verification remains pending.

The final Retry read identified an actual recovery defect beyond artifact
binding idempotency: the API preserves the original output_dir, but fitting
refuses that now-existing directory after a failed registry publication. Two
red-first real-fit regressions reproduced it. The queued adapter now derives
an exclusive UUID5-job/positive-lease-attempt child beneath the configured root.
A new retry job or reclaimed attempt writes fresh files while all prior files
remain byte-identical; same-attempt replay still refuses overwrite. Unleased
adapter calls get a distinct child; direct CLI fitting remains unchanged and
exclusive. Independent expected-outcome/artifact tests passed 60 cases in 5.05s.
The full worker/transport source-bound record is being refreshed after this fix.

## Final post-push master acceptance

The reviewer independently verified all 48 changed source/test/fixture hashes
and sizes, with no omitted changed source files, and reproduced aggregate SHA
`ccda8e1a5f008c450a0002881316bc2ecba8308d17e2b0f5d8d06f344f71e6a1`.
All seven final log hashes match. The correct clean Linux copy is
`runs/post-push-ci-source/source-manifest.json`: all 487 entries match both copy
and current files, manifest SHA
`d848972b6c2cd6000fa121eb9d44e123f287fb86b29a6c883eb537aa071ad5aa`.
The earlier incomplete 384-entry copy was identified explicitly and was not
mistaken for this final tested snapshot.

Final separate results are backend 1,046 passing with nine warnings; worker
1,810 on Windows (65.35s) and clean Linux (44.76s), one optional skip and three
integrations deselected; frontend 566/79 files and all 28 browser flows; all
three actual HTTP transports after the Retry fix in 62.33s; frontend lint/build
with 17 static routes. Parent's global Ruff/format/mypy checks also passed.
Counts from separate runs are not added together.

The master Markdown and JSON accurately distinguish reviewed local code and
protocol checks from real-label/model, full-game/performance, staff and
production obligations. A wording correction narrows idempotency to identical
artifact re-registration; same-attempt fitting still refuses overwrite, while
new-job/reclaimed-attempt Retry preserves failed evidence and succeeds.
The independent master review is accepted for that bounded scope, superseding
the earlier pending notices. This is not full-plan completion, a green remote
CI result or production deployment. No production writes or processor restart
occurred during this review.
