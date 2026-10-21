# Source identity and reviewed outcomes continuation

Date: 2026-10-21. Branch: `codex/overhaul-completion-20261021`.
This report describes implementation and protocol verification, not completed
football acceptance, model accuracy, deployment or staff annotation.

## Full source identity

Registration uses a full SHA-256 through 64 MiB and a sampled registration
fingerprint above that size. `app.services.media_identity` keeps the two meanings
separate. The processor hashes the full actual downloaded source and computes
the exact API-compatible registration fingerprint, with stat checks around the
read. It verifies URI, registration and byte length before dependent stages.
The current authenticated lease publishes a receipt through the existing job
callback before prediction ingest, and checks source stability before cleanup.

Only the configured processor account owning the registered runtime and the
matching unexpired job lease/attempt can create the server-owned immutable
`media:verified:<video-id>` record. Editable video metadata and ordinary settings
cannot supply this proof. A changed URI, size, registration or full hash is
refused. Source attestation survives a later analysis failure/retry; it never
approves a model or makes pending analytics visible. Legacy recordings without
verified registration remain playable and provide no new full-media proof.

Seventeen focused backend proof tests pass. A real disposable PostgreSQL job
callback test verifies that the full hash is available while the pipeline is
still running and survives an analysis retry. Worker tests cover the actual
large-file sampling transition, changed source/registration rejection and the
attestation callback preceding ingest. A native Windows file-URI regression also
found and corrected drive/escaped-space decoding in the shared storage reader.

## Complete annotation snapshots

The worker pages all current human/manual events with deterministic source-time,
frame and ID ordering. The API resolves staff overrides and excludes pending or
superseded output before pagination. Direct staff events with manual provenance
are included even without an attributes-level source marker. The worker checks
the final annotation revision and refuses repeated, malformed, oversized or
changing pages. Limits fail explicitly at 10,000 clips or 100,000 events per clip.
Tests preserve the final event in a 1,201-observation set and reject edits during
snapshot collection. Nothing silently truncates to the former 200-event default.

## Explicit reviewed outcomes

The staff correction form now authors aimed forward-pass targets, pass results,
observed offensive receptions, possession endpoints, carrier cuts and tackle
outcomes. It records the actual named actor/pair, possession, shot and clip clock;
reviewed responsibility, contest and carrier causation require separate explicit
choices. Unknown contest remains unknown. Changing event type clears the prior
confirmation state. Handoff/possession-start forms can carry the same possession
name without changing their existing required fields.

`pipeline.observed_skill_outcomes` consumes those exact events. It provides
target/catch/contest/responsibility outcomes, signed net yards after catch,
instantaneous observed cut locations and reviewed made/missed/assisted tackles.
Negative YAC is retained. Geometry requires supported source points, continuous
known-shot observations and safe calibration. Unknown/conflicting team sides,
contradictory pass results, duplicate conflicting event IDs, cuts after possession
ended and starts from another shot abstain. Motion never assigns responsibility,
tackle success or carrier causation. All new products remain experimental until
the applicable real evidence and review gates pass.

Six actual mounted form POST tests check the serialized input contracts. A real
staff-save API/DB -> event-list -> independent worker subprocess test verifies
the producer contract, actor attribution, negative YAC and experimental status.
Its explicitly invented protocol fixture supplies no football-accuracy evidence.
Twenty-two worker regressions include the independent reviewer's adversarial
cases, reproduced failing before correction. Observed cut maps and supported YAC
paths use existing film drilldowns; location products carry no fabricated scalar.

## Authenticated automatic publication

Approved model metadata alone cannot authenticate a prediction or observation.
Every qualifying automatic real metric now requires the configured processor
principal, its owned registered runtime, a current running pipeline lease with
an actual future expiry, and matching request/body job, video and optional clip.
The API stamps producer identity/attempt and retains pending publication until
normal fenced completion. Ordinary staff retain their human observations and
experimental candidates. Expected predictions still cannot be approved through
the per-observation coach-review endpoint. Historical idempotent receipts remain
read-only. Independent review accepted this boundary; 117 combined source,
publication, serving and real-cohort backend cases pass, including sixteen actual
HTTP producer cases and a trusted pending-to-completion positive.

## Original five-lineman comparisons

The OL analysis screen now consumes actual first-second geometry packets for
the original LT/LG/C/RG/RT small multiples and synchronized trench inspection.
Signed forward/lateral charts share axes. Geometry selects exact supplied source
frames without carrying missing actors forward. Source samples preserve order;
native frame and PTS map one-to-one across actors. Conflicting roles/actors,
source clocks or signed values refuse the affected front. Other clips, videos,
result generations and definitions never form one front. Film inspection seeks
the selected exact recording clock through the existing evidence workflow.
The fixture is actual producer output from an explicitly synthetic contract.
Independent review and 25 focused UI cases pass; this supplies no automatic
role qualification, protection grade or actual football accuracy evidence.

## Reviewed clocks and burned-in preview policy

Time-to-throw now requires one effective reviewed snap and forward release with
explicit recognized clocks, a known common shot and a positive bounded interval.
Duplicate or unresolved human clock IDs, contradictory shot metadata and disputed
clock families refuse qualification. Native frame/PTS evidence remains attached;
automatic aliases cannot replace the reviewed source clock. The observation
contract version invalidates checkpoints computed under the earlier policy.

Optional burned-in previews no longer read raw label values or in-flight numeric
metric flags. Only actual authenticated human/manual Label rows with an annotator
may supply an explicitly attributed staff caption. Clip metadata, per-tracklet
labels without the existing actor binding, disqualified sources and conflicting
staff meanings cannot enter that caption or the label-replay hook. Missing
reviewed captions draw nothing. The ordinary qualified interactive API remains
the source of approved automatic labels and numeric values after publication.

The worker snapshots all human/manual label pages before its annotation-revision
recheck. Stable created-at/ID ordering and revisioned human-label removal prevent
deleted staff meanings surviving pagination or a resumed job. Removal clears the
cached tag, resets review, invalidates measurements and records a nontraining
audit. The overlay generator is now `pts-overlay-v6-native-staff-hud`, so old cached
movies are not reused as newly reviewed previews. Existing historical movies
remain historical; this code change does not rewrite deployed artifacts.

Independent review accepted this scope after reproducing and correcting the
metadata attribution, invalid-source, deletion revision and SQLAlchemy identity
map gaps. Actual encoder tests inspect rendered captions and playable output;
they do not establish actual football labels or qualification.

The real game-window test subsequently exposed a valid sub-output-frame source
window rounding to zero proxy pictures, and the full original's two discarded
preroll packets shifting its displayed-frame origin. Integer native PTS,
discard-packet filtering, exact half-open lookup, at least one picture per
positive source window and bounded late/duplicate decoder handling now address
those reproduced cases. Proxy version 3 and timeline revision 3 invalidate
earlier media. See `native-clock-continuation.md` for measured evidence and limits.

## Combined verification snapshots

Before the final export, tombstone, reliability and original-front additions:
901 backend cases and 1,635 worker cases passed (worker one skipped, two
integration deselected). All 20 browser flows passed, as did the frontend
production build and global ESLint. The later complete frontend rerun passed
511 cases and exposed a duplicated-label test query in the new export component;
the owner corrected it and its focused suite passed. A fresh combined run after
all owners freeze is still required. These distinct snapshots must not be added
together or presented as one final gate.

The separate actual HTTP expected-outcome candidate/verifier/promotion trace is
recorded in `expected-outcomes-http-verification.json`: trusted exact artifact
reload succeeds, ordinary staff with copied live fence fields receive 403,
insufficient release populations fail, and production promotion returns 422.

The platform's remaining code and external acceptance requirements are tracked
in `spec-coverage.md` and `specialist-producer-gap-audit.md`. No reviewed Toledo
240-play benchmark, approved new prediction weights or full GPU throughput gate
has been supplied or manufactured. Production remains the earlier merged code;
this continuation has not been committed, pushed, deployed or used to restart
the separately checked-out long-lived GPU worker.
