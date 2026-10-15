# ADR 0006 — Local libraries, independent recording views, and measured learning

- **Status:** Accepted by the owner for implementation
- **Date:** 2026-10-14
- **Supersedes:** ADR 0001 §6 and ADR 0005 decision 5, which restricted a session to one camera; hosted infrastructure as the default operational requirement
- **Preserves:** American football scope, any-camera ingest, the model router, pgvector, provenance and measurement suppression

## Context

Toledo staff need a usable computer vision film library on ordinary Windows and macOS laptops, including computers without a dedicated GPU. Each staff computer initially owns an independent library. A shared team library is desired later, but it cannot be a prerequisite for filming, reviewing, correcting, or reporting.

Film may arrive from any angle, height or camera. A practice or game may have several independent recordings of the same plays. Treating every recording as a separate play duplicates tendencies. Treating those recordings as synchronized camera rigs invents geometry and identities that the footage does not establish.

Coach feedback must change the current film review immediately. Recording a correction for a future training task while leaving the displayed result unchanged is insufficient. Learning also needs an actual path from reviewed film to materialized data, a fitted candidate, and measured evaluation; counters or placeholder model metrics cannot establish quality.

## Decision

1. **Local operation is the default.** One supervised application runs the frontend, API, Postgres with pgvector, local storage and CPU-capable worker. Jobs remain durable database rows with leases, fencing, checkpoints and explicit pause, retry, cancel and recovery. Cloud hosting remains optional. Automatic heavy training defaults off.

2. **A session contains independent recordings.** `FootballSession` groups recordings, `Play` identifies a canonical play, and `PlayView` associates a recording clip with that play. A single recording works on its own. Staff can manually match views; no automatic synchronization, triangulation or cross-camera identity is implied. Session and report aggregates choose one canonical view, preferring reviewed footage, to avoid duplicate play counts.

3. **Timelines and calibration are explicit.** Recording track observations preserve presentation timestamps where available. Overlay responses identify recording versus clip timebases and the clip offset. Nominal FPS is a marked legacy fallback. Each clip references the exact calibration used for its observations; another recording's calibration cannot establish valid field measurements.

4. **Corrections apply transactionally.** Boundary, identity, tag, event and metadata edits update their effective records and append correction history in the same transaction. Stable operation IDs identify retries; annotation revisions reject conflicting edits. Changed observations invalidate dependent metrics and return the play to review. A tracking issue is a repair flag, not fabricated bounding-box truth. Completing clip review does not automatically validate every computed measurement.

5. **Provenance determines analytical visibility.** Synthetic, missing, superseded and unvalidated outputs cannot become trusted coach metrics through a display toggle. Missing weights leave honest unavailable capabilities. Experimental or heuristic predictions remain distinct from validated measurements. Per-artifact provenance and suppression reasons travel through the API to review.

6. **Learning snapshots contain real data.** Correction export writes versioned local JSONL and hashes. The learning manifest builder joins saved detailed-analysis feature artifacts to reviewed human coverage labels from explicitly selected sessions. It validates source, graph schema, bounds, same-clip association and canonical play grain. Geometry edits advance a separate observation revision, requiring updated graphs before learning; coverage-tag edits can reuse unchanged observations. It rejects conflicts and insufficient data instead of generating examples.

7. **Train, calibration and holdout sessions are disjoint.** The first supported local training variant is `local_coverage`. It requires at least 30 training, 10 calibration and 20 holdout plays, at least two supported coverage classes, and adequate class support in each split. The training dataset's source IDs include training labels only. A separate combined input supplies reserved calibration and holdout labels to the candidate runner; protected calibration/holdout clips are excluded from subsequent ordinary training exports.

8. **Candidates require measured release gates.** The worker fits an actual CPU model and persists its checkpoint, report and holdout predictions under a registered model version. Evaluation compares persisted real predictions against an immutable reviewed human benchmark, treats missing predictions as abstentions, measures accuracy and macro F1, and compares against the training-majority baseline when available. Passing requires at least 20 samples, accuracy ≥ 0.90 and macro F1 ≥ 0.85. Training and calibration clips cannot be release holdouts. Promotion and rollback require a human action and a passing evaluation bound to unchanged artifact bytes and unchanged benchmark revisions.

9. **Portable libraries precede shared hosting.** Session archives preserve film, canonical play relationships and annotation lineage. Import uses stable IDs and does not overwrite existing data. A later team host and synchronization protocol can build on these IDs and idempotent operations; it must preserve independent local use.

## Consequences

The laptop workflow can ingest, group, process, review, correct, export and report without paid GPU infrastructure. It can also prepare a genuine coverage learning cohort, queue CPU training, evaluate a persisted candidate, and explicitly promote or roll back that candidate once enough reviewed film exists.

Accepting arbitrary footage does not guarantee reliable field calibration, readable jerseys, complete tracking through occlusion, or commercial-level recognition quality. The application must expose those limits per recording and result. Other model families require their own actual training adapters and representative benchmarks before entering this release path. Real Toledo benchmark results and hardware trials determine accuracy and throughput; neither UI completeness nor a successful job is evidence that the system outperforms Hudl or SkillCorner.
