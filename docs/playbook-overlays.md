# Playbook overlays & assignment execution scoring (Issue #15)

Turns the call sheet into a Film Room overlay layer: overlay a concept's intended
routes / responsibilities / coaching points on a clip, and grade execution per
assignment and per play — transparently, with confidence and reason codes, and
overridable by a coach. See **ADR 0004** for the decision record.

This document is the contract the frontend (#104 overlay surface) and the GPU
worker consume. Implementation lives in:

- `backend/app/analytics/assignment_scoring.py` — the pure scoring engine.
- `backend/app/routers/playbook.py` — the API (mounted at `/api/v1/playbook`).
- `backend/app/playbook_seed.py` — the two starter concepts.
- `backend/app/models.py` — `PlaybookConcept`, `PlayConcept`, `AssignmentScore`.

## Data model

| Table | Purpose |
|---|---|
| `playbook_concepts` | Coach-authored concept (offense/defense) + assignment defs |
| `play_concepts` | Links a concept to the **clip** (play) it was called on |
| `assignment_scores` | One rule-based grade per assignment per play (+ override) |

A **clip is one play** in this codebase, so concepts link to `clip_id`, and
tracklets (which belong to clips) supply the player trajectories.

### Concept / assignment shape

`playbook_concepts.assignments` is a JSON list of assignment definitions:

```json
{
  "key": "deep_third_middle",
  "role": "Free safety (middle third)",
  "intent": "Carry the deep middle third.",
  "coaching_point": "Stay on top of the post; split the two deepest.",
  "intended_route": { "region": {"x": [0.33, 0.67], "y": [0.0, 0.35]}, "color": "#15397F" },
  "rule": { "type": "zone_landmark", "params": {"region": {"x": [0.33,0.67], "y": [0.0,0.35]}} }
}
```

- `intended_route` is **single-camera, frame-normalized** geometry in `[0,1]`
  (origin top-left). Offensive routes use `points: [[x,y], …]`; zone
  responsibilities use `region: {x:[lo,hi], y:[lo,hi]}`. It is what the overlay
  draws — purely presentational.
- `rule` is the deterministic execution check (separate from the drawn route).

## Scoring rules

| `rule.type` | Checks | Needs calibration? | Key params |
|---|---|---|---|
| `release_direction` | Release along a frame axis/sign | no | `axis`,`sign`,`min_delta`,`window` |
| `zone_landmark` | Finishes inside a normalized region | no | `region`,`tolerance` |
| `depth_threshold` | Reaches N yards downfield | **yes** | `min_depth_yards` |
| `presence` | Tracked for enough of the play | no | `min_points` |
| `manual_only` | Always coach-graded | n/a | — |

Each assignment grade is one of `on_assignment`, `off_assignment`,
`needs_review`, with a soft `score` in `[0,1]` for graded ones (pass threshold
`pass_score`, default 0.6).

### Identity & confidence safety

The engine **never** grades a wrong assignment on a shaky identity:

- No mapping → `needs_review` (`no_player_mapping`).
- Mapped tracklet missing → `needs_review` (`tracklet_not_found`).
- Identity not `known`/`probable`, or below
  `PLAYBOOK_IDENTITY_CONFIDENCE_THRESHOLD` → `needs_review`
  (`low_identity_confidence`).
- Missing trajectory / uncalibrated for a yard rule → `needs_review`
  (`no_trajectory` / `no_calibrated_trajectory`).

`confidence` = product of the identity, track, and (for yard rules) calibration
confidences; `uncertainty = 1 − confidence`. A result is `experimental` unless
the concept is coach-`validated` **and** the identity is `known`; reason codes
`model_identified_concept_unvalidated` and `probable_identity_not_confirmed`
explain the experimental flag. No face recognition is used — identity comes from
the tracklet/Re-ID flywheel and the coach-supplied `assignment_player_map`.

### Assignment→player mapping

`play_concepts.assignment_player_map` maps each assignment `key` to a tracklet
(and optional identity override):

```json
{ "deep_third_middle": { "tracklet_id": "…", "identity_state": "known", "identity_confidence": 0.92 } }
```

A bare `"deep_third_middle": "<tracklet_id>"` is also accepted; a tracklet with a
`player_id` but no override is treated as only `probable`.

## API (all under `/api/v1/playbook`, coaching-staff only)

| Method & path | Purpose |
|---|---|
| `GET /concepts` | List concepts (`?side=`, `?active_only=`) |
| `POST /concepts` | Create a concept (rejects soccer terms; unique name/keys) |
| `POST /concepts/seed` | Install the two starter concepts (idempotent) |
| `GET /concepts/{id}` | Get one concept |
| `PATCH /concepts/{id}` | Edit a concept |
| `POST /clips/{clip_id}/concept` | Link a concept to a clip (upsert) |
| `GET /clips/{clip_id}/overlay` | **Overlay payload**: routes, coaching points, live grades + overrides |
| `POST /clips/{clip_id}/score` | Compute & persist grades for the clip's concepts |
| `PATCH /scores/{score_id}` | Coach override (writes an `assignment_execution` Label) |
| `GET /concepts/{id}/examples` | Example reps (concept-linked + optional embedding expansion) |
| `POST /concepts/{id}/score-corpus` | Re-score all linked clips (**workload-gated**) |

`GET …/overlay` degrades gracefully: a clip with no linked concept returns an
empty `overlays` list with a `reason`; missing substrate yields per-assignment
`needs_review`, never a hard error. The frontend should render `needs_review`
and any `experimental` result with non-definitive copy ("needs review",
"experimental") per the issue's UI-copy constraint.

`GET …/examples` works fully without embeddings; the embedding expansion only
runs when a promoted play-embedding model exists and is always flagged
`experimental` (no new vector store — it reuses #8/#77/#144 infrastructure).

## Governance

- New `Resource.PLAYBOOK` (`read`/`write` → admin/analyst/coach). See
  `docs/governance.md`.
- Audit events (identifiers/enums only): `audit.playbook.concept_upserted`,
  `audit.playbook.concept_linked`, `audit.playbook.scored`,
  `audit.playbook.score_overridden`, `audit.playbook.corpus_scored`.
- Feature flag `PLAYBOOK_ENABLED` (default true) 404s the whole surface when off.

## Settings

| Env var | Default | Meaning |
|---|---|---|
| `PLAYBOOK_ENABLED` | `true` | Dark-launch guard for the whole surface |
| `PLAYBOOK_IDENTITY_CONFIDENCE_THRESHOLD` | `0.6` | Below this (or non-known/probable) → `needs_review`, not a negative grade |
| `PLAYBOOK_SCORE_CORPUS_MAX_PLAYS` | `500` | Cap on clips scanned by `score-corpus` |
