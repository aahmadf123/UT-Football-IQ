# Practice ↔ Game Cross-Regime Self-Distillation (Issue #150 §5.14)

## Why

Football-IQ captures film in **two regimes** (Issue #126):

- **`fixed_sideline`** — game film. Static elevated camera, full field, larger
  players. The *easy* vision problem; detection/tracking confidence is high.
- **`drone_follow`** — practice film. A panning/zooming operator, smaller
  players. The *hard* problem — but it is where Toledo generates **90%+** of its
  footage, and where coaching actually happens.

Hudl, Sportscode, and PFF treat game and practice film as the same problem.
Football-IQ's two-regime architecture lets us exploit the asymmetry:
**self-distill the easy regime into the hard one.** Run the high-quality nightly
pipeline on game clips, treat its output as a *teacher*, and distill it into a
*drone-follow student* detector so practice analytics become as accurate as game
analytics. That is a defensible, Toledo-specific differentiator.

## How a coach tags matched plays

The same play is run in practice and in a game. Coaching staff link the two clips
by giving them the **same play-call code** in `clips.play_call_id` (migration
`0026`, a `String(100)`). Two clips are a "paired play" when they share a
non-null `play_call_id`.

Tag a clip via the existing clip API — the same code on both the practice and the
game clip:

```http
PATCH /api/v1/clips/{practice_clip_id}   {"play_call_id": "Gun Trips Rt 22 Z"}
PATCH /api/v1/clips/{game_clip_id}        {"play_call_id": "Gun Trips Rt 22 Z"}
```

No new tables or services — `play_call_id` rides on the clip row and is returned
in `ClipResponse`.

## The nightly trainer

`gpu-worker/training/` holds two modules (heavy deps — torch/ultralytics — are
imported lazily inside functions, so importing the package is CI-safe):

- **`play_call_aligner.py`** — groups clips by `play_call_id` and pairs the
  `fixed_sideline` clip (teacher) with the `drone_follow` clip (student). A group
  missing a regime, with duplicate regimes, or mislabeled is **skipped and
  logged** so a pair can never invert the two-regime design. `build_pairing_manifest`
  emits an auditable manifest that preserves, for every record: video id, clip
  id, play-call id, capture regime, identity confidence, correction source, and
  validation status.
- **`cross_regime_distill.py`** — the trainer. `distillation_loss` is the
  standard Hinton soft-label KD term:

  ```
  L = alpha · T² · KL(softmax(teacher/T) ‖ softmax(student/T))
      + (1 − alpha) · CE(student, hard_targets)
  ```

  It fine-tunes the drone-follow student supervised by the game pseudo-labels,
  evaluates baseline vs distilled **drone-follow mAP@0.5**, stamps a
  provenance checkpoint, and registers the result in the MLOps registry.

### Guardrails

- **Validated examples only.** `run_nightly` filters to coach-validated/corrected
  pairs (`filter_validated`) — distillation never consumes raw uncertain
  predictions.
- **≥ 100 paired plays.** Training raises if fewer than `MIN_PAIRED_PLAYS`
  validated pairs exist.
- **≥ 5 pp mAP gate.** A checkpoint is `promotable` only when the student's
  drone-follow mAP beats the baseline by ≥ 5 percentage points (`improves_map`).
- **Auditable.** The student is registered via `POST /api/v1/mlops/models` as a
  `detector`, created `experimental`, with metrics carrying `baseline_map`,
  `student_map`, `map_gain_pp`, `n_pairs`, and **both** `teacher_regime` /
  `student_regime`. Promotion to staging/production is a gated human step (the
  backend already requires metrics before promotion).
- **Regime metadata travels with every artifact** — manifest, checkpoint
  provenance, and registry metrics all record the two regimes, so a drone-follow
  student detection can never be confused with a fixed-sideline teacher output.

## Serving the distilled student

The student is registered in the router as `yolov8m-drone-distilled`, on
`NIGHTLY_ONLY_VARIANTS`, and selected by the **regime-aware** resolver
`model_router.select_detect_variant(priority, capture_regime)` — only for nightly
`drone_follow` jobs when `ENABLE_DRONE_DISTILL_NIGHTLY=1`. It never serves
`fixed_sideline` or same-session. See
[`docs/model-routing.md`](model-routing.md#regime-gated-distilled-drone_follow-student-issue-150).

## Running it

```bash
# CI / smoke test — no torch, no GPU, no footage:
python -m training.cross_regime_distill --synthetic --output /tmp/distill.json

# Real run — on the GPU-worker host (pytorch/pytorch:2.5.1-cuda12.4), where torch
# and the footage live. Tag the paired clips first, then:
python -m training.cross_regime_distill \
    --clips coach_tagged_clips.json \
    --video-dir "C:\Additional Storage\Drone Footage" \
    --output distill.json
```

> The real fine-tune cannot run in the cloud Claude session (no GPU/torch, and a
> local Windows footage path is not reachable from the sandbox). It runs on the
> GPU-worker host or after the footage is uploaded to R2 for the deployed nightly
> worker; the nightly invocation rides the existing `nightly-training-exports`
> queue / cron.

## Relationship to the Big Data Bowl adapter (#164)

BDB (`gpu-worker/datasets/bdb/`) is an **offline movement/trajectory prior
only** — clean NFL coordinates marked `offline-pretraining-evaluation-only`. The
actual cross-regime value comes from **paired Toledo practice/game clips by
play-call id**, not BDB. A model that ignores the two-regime design is never
trained or promoted.
