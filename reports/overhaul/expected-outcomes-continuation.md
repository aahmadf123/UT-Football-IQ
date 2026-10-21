# Expected outcomes continuation — 2026-10-21

Executable candidate preparation, fitting, evaluation, worker inference and staff learning controls are implemented for four tasks. No staff-reviewed football dataset was available for fitting or acceptance in this work. All numeric test fixtures exercise protocol mechanics only; their results are not model accuracy evidence. No production weights, fabricated staff labels, promotion, deployment or accuracy claim were created.

## Public artifact and stage contract

`gpu-worker/pipeline/expected_outcomes/data.py` defines feature schema `expected-outcomes-2`. Its immutable snapshots require real source attribution, named prediction event/native seconds, qualified feature values with exact units and evidence hashes, session/recording/full-media-SHA/canonical-play lineage and an explicit task subject. Optional game lineage remains distinct from the required session. Every training row additionally requires a human target, actual reviewer ID, timezone-qualified review timestamp, annotation SHA and explicit train/calibration/holdout split.

| Task / metric name | Prediction clock / subject | Features | Output scalar / unit |
| --- | --- | --- | --- |
| `completion` / `expected_completion` | Forward pass release / actual QB track | Target distance to QB; nearest defender distance, yards | `value` / `prob` |
| `rushing_gain` / `expected_rushing_gain` | Designed-rush handoff / actual rusher track | Past-only runner speed, yd/s; nearest defender distance, yards | `value` / `yd` |
| `yac` / `expected_yac` | Observed completed forward-pass catch by offense / actual receiver track | Past-only runner speed, yd/s; nearest defender distance, yards | `value` / `yd` |
| `pressure` / `contextual_pressure` | Pass-play snap; pressure within 2.5 seconds is the reviewed target / explicit team scope | Measured original-OL spacing, explicit DL/LB role counts/depths and gap alignment, box count, actual called rush/protection/blitz counts, down and distance | `value` / `prob` |

Pressure schema version 2 requires reviewed original LT/LG/C/RG/RT identities, the complete explicitly named defensive-role population and actual call context known before the snap. A shallow defender is not inferred to be a linebacker or blitzer. Original OL spacing measures player-center distances, not body-edge lane widths. Missing roles, alignment or calls suppress the snapshot. Existing coefficients with different feature semantics cannot load under version 2.

`pipeline.expected_outcomes.extract.extract_snapshot()` consumes qualified existing events and tracklets, plus normal lineage and an optional verified `VideoTimeline`. It requires exact observed/visible field points in a common known shot at the prediction clock. It never interpolates a future point, fills unknown counts with zero, guesses a receiver, or exports a known synthetic/stub/test observation as real. Runner speed uses a connected earlier observation. Explicit eligibility rejects interceptions/backward passes and known contradictory offense/defense identities. Feature evidence contains only used observations and available context; future trajectory and target outcomes are excluded.

Named prediction clocks retain distinct reviewed EventIDs during reconciliation. One qualified reviewed release takes precedence over automatic release aliases; multiple reviewed clocks remain ambiguous and suppress the task. Refused staff clock evidence carries an in-memory review-required family marker so an approved machine clock cannot silently replace it. Explicit nested feature/call context timebase tags must be `recording` and are included in hashed evidence; legacy absent tags retain recording-second semantics.

`pipeline.stage_expected_outcomes.run(clip_id, job_id, priority, *, tracklets, events, lineage, analytics_safe, timeline, attacking_direction, down, distance_yards, capture_regime, snapshots=None, model_paths=None, persist=True)` returns `predictions`, `feature_snapshots`, `model_routing` and clip identity. Each prediction preserves real weights SHA, feature version, population, capture regime, lineage, named observation clock, feature evidence hashes and subject. Player predictions persist the actual `tracklet_id`; pressure stays explicitly team-scoped. Missing features/model/approval produce a suppressed null with reason, never a baseline estimate. Offline `model_paths` always produces experimental output.

The stage is present in router defaults/configuration, quick/detailed stage lists, orchestration dispatch and checkpoint identity. Production model types are the four metric names above. Checkpoints include feature definitions and registry artifact/approval identity so changed definitions or approval state cannot reuse stale stage output. Existing reviewed-role hydration supplies only qualified observations; roster membership is not feature evidence.

## Staff dataset handoff

Normal pipeline completion now exports the actual `expected_outcomes.feature_snapshots`, including when prediction models are unavailable, through `pipeline/expected_outcomes/export.py`. The immutable canonical JSON lives at `artifacts/training_features/expected_outcomes/<video-id>/<sha256>.json`; the bounded job summary retains only its URI, SHA-256, byte count, snapshot count and source identity. Original typed feature evidence, full media SHA, verified native source clock, lineage and provenance remain in the downloaded document. No targets or review attestations are filled.

In Learning → expected outcomes, staff sees the recording filename and snapshot count, requests an authenticated download, and receives a signed attachment named `expected-features-<job-id>.json`. Both link creation and file download require that exporting job's protected configured-processor/current-attempt media attestation. A copied ordinary staff job output cannot qualify through an earlier global media proof. The reader accepts only the exact owned content-addressed local/S3 artifact and rechecks full source/canonical/session/clock bounds, active clip, and frozen annotation/observation revisions. Corrections or superseded clips require fresh processing. The dedicated attachment response preserves the advertised filename across origins and uses `private, no-store`.

`backend/tests/test_expected_feature_export.py` exercises actual normal worker extraction and storage, trusted fenced HTTP job completion, authenticated signed attachment bytes/filename, and the real `targets-template` command. It checks local and mocked S3 reads, rejects copied untrusted jobs and bad signatures/hashes, and refuses annotation/observation changes and recut clips. The generated protocol fixture is not real football evidence. `test_normal_expected_stage_exports_current_review_scope` covers orchestration export reachability; summary compaction retains the bounded descriptor.

The helper is usable where actual qualified stage `feature_snapshots` exist. It does not author missing football feature context or review attestations. From `gpu-worker`, use its existing Python environment:

```powershell
.venv/Scripts/python.exe -m training.expected_outcomes targets-template `
  --snapshots C:/review/expected-stage.json --snapshots-sha256 <export-sha256> `
  --output C:/review/staff-targets.csv
```

Staff must enter actual outcome, `source=human`, `reviewed=true`, reviewer ID, ISO review time and split in that CSV. Target and review cells are initially blank. Repeated `--snapshots`/`--snapshots-sha256` pairs accept multiple immutable stage exports. Preserve their source media. Obtain hashes with `Get-FileHash -Algorithm SHA256` and lowercase the resulting digest for the command arguments.

```powershell
.venv/Scripts/python.exe -m training.expected_outcomes prepare `
  --snapshots C:/review/expected-stage.json --snapshots-sha256 <export-sha256> `
  --annotations C:/review/staff-targets.csv --annotations-sha256 <csv-sha256> `
  --media C:/film/recording-one.mp4 --media C:/film/recording-two.mp4 `
  --output C:/review/new-frozen-dataset
```

`prepare` joins each staff row to exact task/canonical-play/evidence-hash/native-clock keys, verifies original media hashes and writes typed feature/annotation files, dataset JSONL and a hash-bound manifest to a new directory. Use enough disjoint actual session/game groups for all three splits; two media files in this command are syntax examples, not a sufficient validation population. Existing paths, blank/unreviewed targets, wrong hashes, unknown feature values, leakage and output aliases are refused. Optional `--population <reviewed-json> --population-sha256 <sha256>` copies explicit reviewed release-population attestations; it invents none.

```powershell
.venv/Scripts/python.exe -m training.expected_outcomes fit `
  --manifest C:/review/new-frozen-dataset/manifest.json --manifest-sha256 <manifest-sha256> `
  --output C:/review/new-candidate --task completion

.venv/Scripts/python.exe -m training.expected_outcomes evaluate `
  --manifest C:/review/new-frozen-dataset/manifest.json --manifest-sha256 <manifest-sha256> `
  --model C:/review/new-candidate/completion.checkpoint.json --model-sha256 <actual-model-sha256> `
  --output C:/review/new-independent-report.json
```

Omitting `--task` fits each available supported task. Fitting never reads holdout labels for coefficients or calibration. Completion/pressure reuse the existing logistic pressure classifier and Platt calibration; regression uses deterministic linear fitting with calibration-set bias correction. Reports compare complete immutable holdout Brier/log loss/ECE or MAE/RMSE/outcome calibration with the empirical training-population mean. A report never self-approves its checkpoint.

## Existing learning workflow and independent verification

The learning UI exposes manual expected-outcome queueing with worker-local manifest path/hash, new candidate folder and optional task. `POST /api/v1/learning/train` accepts `training_variant=expected_outcomes` and these fields. The API validates shape and preserves Windows paths verbatim; the connected worker authoritatively opens/checks them. It does not try to resolve laptop files on a hosted API. Existing automatic-training defaults remain unchanged. Requests and reports retain job ID and manifest checksum, including a separate labeled recent-request view when the current form changes.

The existing train dispatcher reaches `train_expected_outcomes_job`; successful training uploads actual JSON checkpoint, report and frozen release evidence through the configured local/S3-compatible artifact store, then registers only experimental candidates. Release evidence includes exact typed inputs and original source references, not just asserted hashes. Output folders/files use exclusive creation.

`POST /api/v1/learning/expected-outcomes/{model_id}/evaluate` freezes candidate/report/release-evidence/dataset hashes in a server challenge and manually queues a separate `verify_expected_outcomes` job. Its processor reloads the exact checkpoint, reopens/hashes original film, verifies native PTS, checks stable file identity around hash/probe, validates exact reviewed feature/target artifact bindings and all three immutable split memberships, and recomputes every heldout forward output from feature-only inputs. Submitted predictions cannot substitute for actual model computation. The API performs bounded artifact hashing/parsing and statistical checks only; it never loads/runs model inference.

The receipt binds challenge, model, task, evidence hashes, job, current worker and attempt. The API accepts completion only from the configured processor service principal owning that runtime under its current live lease; ordinary staff job listings or copied receipt URIs cannot confer this authority. A later explicit UI/API action records the completed evaluation. The existing admin human model-promotion decision rechecks exact artifact/evaluation/receipt/current challenge and gates. Missing or changed film/model/report, stale attempts, copied output, wrong task/split/calibration and raw audit-only approval are refused.

Full release requires the immutable 240 canonical heldout plays / 12 independently confirmed source groups contract, with at least 80 plays and four sources in each practice/coach-game/television category and at least 20% double-labeled/adjudicated examples with distinct reviewers. Actual recording/full-media membership is disjoint across confirmed groups. Capture regime is separately approved. Candidate loss must beat baseline and calibration must not worsen (binary ECE also has an absolute 0.1 bound). Small experimental evaluations report missing-data reasons and cannot promote.

Serving additionally binds each approved prediction to actual server Video/session/full-media identity, the clip's canonical PlayView grouping, source clock inside the clip, approved feature/category slice and actual clip-owned task actor. Manual-source metric posting cannot bypass this gate. The shared trusted full-media identity producer distinguishes registration fingerprints from actual full-content SHA for large recordings. Unknown verified identity abstains. Alternate views retain their own source/clock/track identities while sharing canonical play grouping.

## Validation and remaining evidence gates

Focused expected-outcome worker tests pass 51 cases. The expected/router/registry/queue/orchestrator regression passes 164 cases. Backend focused release/training/ingest tests pass 49 with the shared media-identity helper integration; strict scoped mypy passes six touched backend modules, and scoped Ruff checks pass. Mounted expected-outcome/existing-learning UI tests pass eight cases; frontend typecheck and scoped ESLint pass. The final full backend/frontend/browser verification is coordinated by the parent task. The actual PostgreSQL player-cohort ingest/query regression was appended for that run. Numeric fixtures and tiny generated test videos are test mechanics only and never count toward football evidence.

Actual transport verification also passes: `tests/integration/test_expected_outcomes_http.py` starts a disposable loopback API against the isolated completion PostgreSQL database, queues real candidate training over HTTP, registers the trusted processor runtime, claims live leases over HTTP, runs the real training/upload/registry adapter, freezes and queues an independent verifier challenge, reloads the actual checkpoint/media/native clocks and completes through the authenticated HTTP receipt boundary. Ordinary staff copying the live worker/fence fields receive 403. The trusted completion records a failed small-population evaluation, and production promotion receives 422. The final run passed in 22.56 seconds. Machine-readable trace: [expected-outcomes-http-verification.json](expected-outcomes-http-verification.json). It is explicitly synthetic protocol evidence, not football validation. Each run uses a unique manifest identity so an old registered candidate cannot accidentally supply a prior temporary store's artifacts.

Remaining external gates: actual qualified feature coverage and staff targets; complete independently reviewed/disjoint release populations for each task and applicable strata; real artifact fitting; trusted connected-worker verification; explicit human promotion; and production cohort/canary evaluation. These code paths do not establish model accuracy, full platform acceptance or production readiness without that evidence.
