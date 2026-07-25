# ADR 0004 — Playbook overlays & assignment execution scoring

Status: **Accepted** (Issue #15)
Date: 2026-06-01
Deciders: Football-IQ analytics/UX
Related: #8/#77 (play embeddings), #74 (SAM masks), #101 (single camera), #104
(clip review overlay layer), #110/ADR 0001 (session/side-of-ball), #127/#128/#129
(calibrated tracking), #137 (tendency breaks), #144 (concept search), #166
(external-resource governance)

## Context

Issue #15 asks us to turn the call sheet into a living layer of the film room:
overlay a concept's intended routes / responsibilities / coaching points on the
clip, and give a transparent execution grade per assignment and per play. The
owner's Phase-CV review re-scoped it to **stack on** existing Phase-CV outputs
rather than re-design them, in four steps: (1) a light, coach-edited call-sheet →
concept mapping, (2) wire the overlay for one offensive concept + one defensive
structure on the existing #104 surface, (3) **rule-based** (no-ML) execution
scoring on the Phase-CV substrate, (4) a coach-override flow that feeds future
training.

A later backlog comment added hard constraints: scoring must consume identity /
track / calibration / classification confidence; a low-confidence or conflicting
identity must **never** be graded a "wrong assignment"; pretrained/zero-shot
outputs must be marked experimental until validated against corrected Toledo
clips; corrections must persist; and no face recognition.

## Decision

### 1. Three light tables, no new vector store

- `playbook_concepts` — coach-authored concepts (offense/defense) and their
  assignment definitions (intended route, coaching point, scoring rule).
- `play_concepts` — links a concept to the **clip** it was called on (a clip is
  one play in this codebase), with the assignment→tracklet map and a
  coach-`validated` flag.
- `assignment_scores` — one rule-based grade per assignment per play, with
  confidence/uncertainty, reason codes, and a coach-override surface.

We deliberately **do not** add a second vector database. Concept "example reps"
reuse the existing pgvector play-embedding search (#8/#77) with a graceful
fallback to concept-linked clips, exactly as concept search (#144) does.

### 2. Rule-based, confidence-aware scoring engine

`app/analytics/assignment_scoring.py` is a pure, dependency-free engine (sibling
of the tendency-break engine). Each assignment carries a small deterministic
`rule` (`release_direction`, `zone_landmark`, `depth_threshold`, `presence`,
`manual_only`) evaluated against whatever substrate exists. The engine:

- returns `needs_review` (never a negative grade) when the assignment→player
  mapping is missing, the tracklet is gone, the identity is not `known`/`probable`
  or below `PLAYBOOK_IDENTITY_CONFIDENCE_THRESHOLD`, or the required trajectory /
  calibration is absent;
- emits machine-readable reason codes and a confidence (product of the identity,
  track and — where the rule needs yards — calibration confidences) with its
  complementary uncertainty;
- flags results `experimental` unless the concept is coach-validated **and** the
  identity is `known`, and adds reason codes for model-identified concepts and
  merely-`probable` identities.

The overlay's `rule` is intentionally separate from the drawn `intended_route`:
the route is what the coach draws on film; the rule is the check used to grade.

### 3. Single-camera overlay geometry

Overlay geometry (`intended_route`) is single-camera, frame-normalized `[0,1]`
so the existing #104 render-layer can draw it with render-layer toggles — **no**
multi-angle camera switching (#101 / ADR 0001).

### 4. Optional, not required, calibrated tracking

Yard-based rules (`depth_threshold`) need a calibrated trajectory and degrade to
`needs_review` without one. Calibrated tracking (#127/#128/#129) is therefore an
*optional* quality enrichment, never a hard dependency — overlays and the other
rules work on frame geometry alone. SAM masks (#74) are likewise not required.

### 5. Coaching-staff only; coach overrides feed training

The whole surface is gated by a new `Resource.PLAYBOOK` (`read`/`write` →
admin/analyst/coach). Execution scores are never exposed to player/viewer
accounts (Non-Goal: no player-facing scores without coach mediation). A coach
override (`PATCH /scores/{id}`) is authoritative on the row **and** writes an
`assignment_execution` training Label so corrections feed the flywheel.

### 6. One heavy endpoint is workload-gated

`POST /concepts/{id}/score-corpus` scans every linked clip, so it is
`require_workload_capacity("playbook.score_corpus")`-gated and capped by
`PLAYBOOK_SCORE_CORPUS_MAX_PLAYS`. Per-play scoring stays ungated (cheap).

## Consequences

- ✅ Coaches get overlays + explainable, confidence-scored grades that stack on
  Phase CV without re-designing it, and that degrade honestly when the substrate
  is thin (no mock data presented as real).
- ✅ No new vector store; no new external resource (rule-based, pure-Python), so
  the #166 external-resource gate adds **no** LICENSES.md row.
- ✅ Identity safety and experimental labelling are enforced in one engine that
  is unit-tested independent of the DB.
- ➖ Frame-normalized rules are coarse without calibration; richer geometry
  arrives for free once calibrated trajectories are populated.
- ➖ The Film Room overlay **rendering** (frontend) is a follow-up; this ADR and
  PR deliver the data model, engine, governance, and the overlay/score API
  contract the #104 surface consumes.

## Follow-up

- Render the overlay + scores + override control in the Film Room (#104 surface).
- Replace the rule-based grade with a learned scoring head once enough coach
  corrections accumulate (the override Labels are the training source).
- Auto-identify the called concept (currently coach-linked or `source="model"`).
