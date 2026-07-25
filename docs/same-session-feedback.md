# Same-session feedback loop — clip result tier (Issue #147)

The same-session lane (period-break clips, priority `10`) delivers a tagged
first pass within the 90-second SLO described in §5.11 of the research report.
Those first-pass results are intentionally *lighter* than the nightly
full-quality run (lighter model variants, fewer stages — see
[`docs/model-routing.md`](model-routing.md) and
`gpu-worker/pipeline/lightweight_config.py`). A coach therefore needs to know,
per clip, **whether they are looking at the preliminary pass or the final
result**, and where the clip sits in the review queue.

This page documents the clip-level result tier that carries that signal. It does
**not** change the model-router contract, add a vector store, or introduce a new
SAM/multi-camera path — it is a thin status field layered on the existing
same-session / nightly split.

## Result tier: `preliminary` vs `final`

`clips.result_state` (`ClipResultState`, stored as a plain `VARCHAR(20)` like
`processing_jobs.pipeline_mode`):

| Value         | Meaning                                                                 |
| ------------- | ----------------------------------------------------------------------- |
| `preliminary` | Produced by the same-session lightweight path; awaiting nightly upgrade |
| `final`       | Produced or upgraded by the nightly full-quality path                   |
| `NULL`        | Legacy / unknown (predates this column) — **not** treated as preliminary |

The GPU worker's `stage_segment` tags every clip it creates from the job
priority (`gpu-worker/pipeline/stage_segment.py`):

```text
priority >= 10 (same-session) → result_state = "preliminary"
priority <  10 (nightly)      → result_state = "final"
```

This rides on the existing `select_model` priority threshold via
`pipeline.model_router.is_same_session` — no new routing knob.

## Nightly upgrade — replacing preliminary results

When nightly full-quality processing for a video lands, the same-session first
pass is superseded. The worker calls
`POST /api/v1/videos/{video_id}/clips/finalize` (after a nightly `render`
completes; `backend.finalize_video_clips`), which flips every `preliminary`
clip on that video to `final`:

- **Idempotent** — clips already `final` (or legacy `NULL`) are untouched, so a
  re-run finalizes nothing.
- **Best-effort** on the worker side (wrapped + logged), mirroring the existing
  nightly-HLS follow-up.
- Once finalized, the coach's "Preliminary" badge clears.

## Derived review state (no extra columns)

`ClipResponse` exposes two derived, read-only fields alongside the raw
`result_state`:

- `is_preliminary` — `result_state == "preliminary"`.
- `review_state` — one of `reviewed` / `low_confidence` / `needs_review`, with
  this precedence (`app.routers.clips._derive_review_state`):
  1. `reviewed` — a coach has signed off (`is_reviewed`); wins outright.
  2. `low_confidence` — clip `confidence` below
     `CLIP_LOW_CONFIDENCE_THRESHOLD` (default `0.5`), **or** calibrated high
     uncertainty (`uncertainty_calibrated` + `uncertainty_score`, Issue #146).
     An *uncalibrated* uncertainty score is never treated as trusted.
  3. `needs_review` — a confident first pass nobody has confirmed yet.

These map onto the result-payload states the issue calls for — processed
(`final`), queued/processing (job lifecycle in the Practice Inbox), failed (job
status), low-confidence, and needs-review — without inventing a second state
machine: clip *quality tier* (`result_state`) and *review status*
(`review_state`) stay orthogonal, and job *lifecycle* remains on
`processing_jobs`.

## Where it surfaces

- **Practice Inbox** (`/api/v1/inbox/status` → `VideoInboxItem.preliminary_clip_count`):
  a "Preliminary" badge plus an "N preliminary clips — nightly upgrade pending"
  note on each video row.
- **Clip review** (`frontend/src/app/clip-review`): a "Preliminary" pill and a
  review-state pill (`frontend/src/components/clip-state-badge.tsx`) next to the
  play title, and a "Results: Preliminary (same-session) / Final (nightly)"
  metadata row.

## Identity degrades gracefully

No face recognition or manual roster mapping is required before a clip enters
the same-session path. Re-ID (`gpu-worker/pipeline/stage_reid.py`) only ever
*fills* an existing `player_id` and leaves unknown tracks as low-confidence
track IDs — a same-session clip is `preliminary` / `needs_review`, never blocked
on identity. Coach corrections on these clips feed the active-learning queue
exactly as before (Issues #145/#146).

## Configuration

| Env var                         | Default | Read by | Purpose                                   |
| ------------------------------- | ------- | ------- | ----------------------------------------- |
| `CLIP_LOW_CONFIDENCE_THRESHOLD` | `0.5`   | backend | Threshold for the `low_confidence` review state |

No vendor keys are involved; nothing here reaches frontend bundles or logs
beyond the derived, non-sensitive state strings.
