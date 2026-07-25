# Model Routing

Football-IQ's GPU worker chooses model variants per pipeline stage based on
the priority of the processing job. Same-session jobs (period-break clips,
priority `10`) must hit the 5–10 minute feedback window, so they route to
lighter variants. Nightly jobs (priority `0`) can spend more compute for
higher quality.

Routing lives in `gpu-worker/pipeline/model_router.py`. Stages call
`select_model(stage, priority)` and get back a variant identifier.

## Default routing table

| Stage        | Same-session (priority ≥ 10) | Nightly (priority < 10) |
| ------------ | ---------------------------- | ----------------------- |
| `segment`    | `optical-flow-fast`          | `optical-flow-fast`     |
| `calibrate`  | `calib-hough-dlt`            | `calib-hough-dlt-kalman`|
| `detect`     | `yolov8n`                    | `yolov8m`               |
| `ball`       | `yolov8n-ball`               | `yolov8n-ball`          |
| `track`      | `iou-tracker`                | `iou-tracker`           |
| `reid`       | `jersey-ocr`                 | `parseq-ocr`            |
| `pose`       | `rtmpose-t`                  | `rtmpose-m`             |
| `render`     | `ffmpeg-overlay`             | `ffmpeg-overlay`        |
| `embeddings` | `none`                       | `play-embed-clip-vitb32-baseline` |

The pose row is the contract preserved from issue #16: same-session pose
jobs route to RTMPose-tiny (~1000 FPS on a GTX 1660 Ti), nightly pose jobs
route to RTMPose-medium (~430 FPS).

## Which variants are safe for same-session use

A variant is "same-session safe" if a clip-length job completes inside the
period-break window on the production GPU. Today that means:

- Detection: YOLOv8n is the cap. Anything larger goes to nightly.
- Pose: RTMPose-tiny only. Heavier RTMPose / ViTPose variants are nightly.
- Segment / track / reid / render: current defaults are already fast
  enough that the same variant runs for both buckets.
- Experimental models (e.g. anything queued for issues #74–#76) must
  default to nightly until benchmarked.

When in doubt, route experimental models to `nightly` and let them prove
out before promoting them to `same_session`.

The router maintains `NIGHTLY_ONLY_VARIANTS` (currently `{"sam3.1",
"sam3-mask-tracker", "play-embed-clip-vitb32-baseline", "botsort",
"strongsort", "parseq-ocr", "yolov8m-drone-distilled"}`). Any routing config —
env override or
otherwise — that tries to place one of these in the same-session bucket
is rejected at load time and the bucket falls back to the bundled
default. This is the hard guardrail behind the "experimental models
default to nightly" rule above.

## Detection: players, ball, officials (Issues #128 / #133 / #148)

Detection is **regime-aware**: it branches on the `capture_regime` detected at
ingest (Issue #126), exactly like `calibrate`. The model router still owns the
*model* choice; the *slicing strategy* is chosen at the stage call site from
the regime, so `select_model` stays purely priority-keyed.

| Stage | What the router picks | `drone_follow` strategy | `fixed_sideline` / `unknown` |
| ----- | --------------------- | ----------------------- | ---------------------------- |
| `detect` (player) | `yolov8n` / `yolov8m` | dual-resolution + SAHI 400 px tiles (0.2 overlap) — recovers 30–80 px players | base detector, full frame |
| `ball` | `yolov8n-ball` (dedicated nano model) | SAHI 128 px tiles (0.2 overlap) — recovers the 6–18 px ball | base detector, full frame (~3× faster) |

- **Why player detect stays `yolov8n`/`yolov8m`.** SAHI and dual-resolution
  are *inference strategies* wrapped around the router-resolved base detector
  (`pipeline.detection.sahi_wrapper`, `pipeline.detection.dual_res_merger`),
  not new variants. The same-session VRAM ceiling keeps YOLOv8n as the
  same-session cap; YOLO11m / RF-DETR-L (Issue #128) are future variants that
  must clear a benchmark before promotion, per the "experimental → nightly"
  rule above. Wrapping a router-resolved adapter (never a raw YOLO handle)
  preserves same-session safety.
- **Ball is a separate model, not a player class** (Issue #133): the
  player/ball class imbalance is too severe for a shared head. `ball` runs the
  same nano model in both priority buckets; SAHI is gated by regime, not
  priority, and stays under the 1.5 GB same-session add-on budget. The ball
  variant is **not** on `NIGHTLY_ONLY_VARIANTS`.
- **Official suppression** (Issue #148): striped officials are relabeled
  `class = "official"` (and off-field figures `"sideline"` when a field
  polygon is supplied) by `pipeline.detection.official_suppressor`. It
  relabels — never deletes — so a false positive costs a relabel, not a lost
  player. `stage_labels` filters `official` / `sideline` before formation,
  personnel, and team analysis.

The per-job audit records the model choices in
`output_artifacts["model_routing"]` (`{"detect": ..., "ball": ...}`) and the
regime/slicing detail in `output_artifacts["detection_strategy"]`.

## Calibration variants (Issue #127)

The `calibrate` stage is regime-aware (it branches on the `capture_regime`
detected at ingest, Issue #126) and pixel-only — both variants are pure
OpenCV/NumPy paths, so neither is on the `NIGHTLY_ONLY_VARIANTS` guardrail:

- `calib-hough-dlt` (same-session): white-paint detection → Hough →
  angle clustering → labeled yard-line correspondences → normalized
  DLT + RANSAC, with a 5-component confidence score. Fast enough for the
  period-break window.
- `calib-hough-dlt-kalman` (nightly): the same detection core plus a
  9-DoF Kalman smoother over the per-window homography series for
  `drone_follow` clips, and chained-ECC drift (Issue #138) as the
  temporal-stability signal.

For `fixed_sideline` (game) film the camera is effectively bolted down, so a
single homography is fit once on the cleanest frame and flagged
`is_game_anchor`; same-session jobs can reuse that cached anchor instead of
recomputing. The deep-keypoint upgrade (PnLCalib / No-Bells-Just-Whistles)
referenced in Issue #127 is a future nightly-only variant and is **not** yet
bundled. See [`docs/calibration-contract.md`](calibration-contract.md) for the
full calibration contract.

## Nightly-only: play embeddings (Issue #8)

`embeddings` is a nightly-only stage. Same-session jobs return the
sentinel `"none"` so the embed stage is a no-op inside the period-break
window — the "find me reps like this" coach flow operates on
previously-ingested clips, so there is no value in spending
period-break GPU budget on a new clip the coach hasn't watched yet.

The nightly variant is `play-embed-clip-vitb32-baseline`. It fits in
~1.5 GB VRAM (CLIP ViT-B/32 + a small structured projector) so it
cohabitates the 16 GB nightly bucket comfortably with YOLOv8m +
RTMPose-m. SAM 3.1 (when `ENABLE_SAM3_NIGHTLY=1`) and `stage_embed`
must not share a job slot — schedule them in separate slots to stay
under the ceiling. See `docs/embeddings-architecture.md` §11 for the
full rationale.

## Experimental nightly: SAM 3.1 (Issue #74)

Set `ENABLE_SAM3_NIGHTLY=1` in the worker env to upgrade the nightly
buckets for `detect` and `track`:

| Stage | Same-session | Nightly (flag off) | Nightly (flag on) |
| ----- | ------------ | ------------------ | ----------------- |
| `detect` | `yolov8n` | `yolov8m` | `sam3.1` |
| `track`  | `iou-tracker` | `iou-tracker` | `sam3-mask-tracker` |

The flag only affects the nightly bucket; same-session always uses
YOLOv8n + IoU regardless of its value. SAM 3.1 weights are gated on
Hugging Face — the worker reads `HF_TOKEN` at runtime to download them
and logs a warning if the token is absent. See
`reports/phase2-issue74-sam3-eval.md` for the eval harness and
promotion criteria.

## Tracker adapters: BoT-SORT / StrongSORT (Issue #129)

The same-session `track` bucket stays on `iou-tracker` — the lightweight,
predictable path that fits the period-break window. Two heavier adapters are
available **nightly only** and live in `gpu-worker/pipeline/tracking/`:

| Variant | What it adds | VRAM | Routing |
| ------- | ------------ | ---- | ------- |
| `botsort` | constant-velocity prediction + ECC camera-motion compensation + optional appearance ReID — survives `drone_follow` pan | ~2 GB | nightly via `ENABLE_BOTSORT_NIGHTLY=1` |
| `strongsort` | matching cascade + appearance-EMA, best offline IDF1 | ~3 GB | nightly via `MODEL_ROUTING_CONFIG` override |

Both are on `NIGHTLY_ONLY_VARIANTS`, so a config override can never route them
to same-session. They are pure-NumPy and carry **no model weights**: BoT-SORT's
camera-motion warps and both adapters' appearance embeddings are *optional
injected inputs* (the ECC warps come from
`pipeline.homography.camera_motion_ecc`; embeddings ride on the detection dicts
when a ReID adapter produced them). With neither supplied, the adapters degrade
to a constant-velocity, IoU-scored tracker — strictly no worse than
`iou-tracker` and better through detection gaps.

Set `ENABLE_BOTSORT_NIGHTLY=1` to upgrade the nightly `track` bucket to
BoT-SORT:

| Stage | Same-session | Nightly (flags off) | `ENABLE_BOTSORT_NIGHTLY=1` |
| ----- | ------------ | ------------------- | -------------------------- |
| `track` | `iou-tracker` | `iou-tracker` | `botsort` |

**Precedence.** If both `ENABLE_BOTSORT_NIGHTLY` and `ENABLE_SAM3_NIGHTLY` are
set, the SAM 3.1 **mask** tracker (`sam3-mask-tracker`) wins the nightly track
slot — it is tied to SAM 3.1's mask detections. BoT-SORT is applied first and
SAM 3.1 overrides it. Same-session is never affected by either flag.

BoT-SORT and StrongSORT must clear the Issue #129 acceptance benchmark
(ID-switches < 5/play, IDF1 > 75, same-session VRAM < 6 GB) before either is
promoted to a same-session default; until then they stay nightly-only.

## Regime-gated: distilled DRONE_FOLLOW student (Issue #150)

`yolov8m-drone-distilled` is the cross-regime self-distilled `detect` student:
the nightly trainer (`gpu-worker/training/cross_regime_distill.py`) distills the
high-quality FIXED_SIDELINE game pipeline (teacher) into the harder
`drone_follow` practice regime (student). See
[`docs/cross-regime-distillation.md`](cross-regime-distillation.md).

Unlike the SAM 3.1 / BoT-SORT nightly swaps, this variant is **not** a
routing-table swap, because it is **regime-specific** — handing a `fixed_sideline`
clip a drone-tuned detector would degrade game detection and violate the
two-regime design. Instead the router exposes a regime-aware resolver:

```python
model_router.select_detect_variant(priority, capture_regime)
```

It returns `yolov8m-drone-distilled` **only** when the job is nightly **and**
`capture_regime == "drone_follow"` **and** `ENABLE_DRONE_DISTILL_NIGHTLY=1`;
otherwise it delegates to `select_model("detect", priority)`. The detect
dispatch in `gpu-worker/__main__.py` calls this resolver, so the per-job
`output_artifacts["model_routing"]["detect"]` records the variant that actually
ran. `select_model`'s signature is unchanged.

| Stage | Same-session | Nightly `fixed_sideline` | Nightly `drone_follow` + `ENABLE_DRONE_DISTILL_NIGHTLY=1` |
|---|---|---|---|
| `detect` | `yolov8n` | `yolov8m` | `yolov8m-drone-distilled` |

The variant is on `NIGHTLY_ONLY_VARIANTS` (never same-session) and the flag is
off by default: the distilled student must clear a **≥ 5 pp drone-follow
detection mAP gain** over baseline and be promoted past `experimental` in the
MLOps registry before it is trusted.

## Re-ID upgrade: PARSeq + trajectory prior + min-cost flow (Issue #131)

`reid` now routes `jersey-ocr` (Tesseract) for same-session and `parseq-ocr`
(PARSeq) for nightly. `parseq-ocr` is on `NIGHTLY_ONLY_VARIANTS`. The PARSeq
adapter reads small / rotated / motion-blurred jersey numbers far better than
Tesseract and **falls back to Tesseract at runtime** when its checkpoint is
absent (activated by `REID_OCR_MODEL=parseq:/path/to/parseq.pt`; no weights are
committed). The audit still records the *routed* variant (`parseq-ocr`) even
when the fallback fires — same convention as `detect` recording `yolov8m`.

`stage_reid` applies four layers in priority order, all of which only ever fill
the existing `player_id` via `PATCH /api/v1/tracklets/{id}` (no tracklet schema
change, single-camera only):

1. **OCR** — PARSeq (nightly) or Tesseract (same-session).
2. **Appearance gallery** — cosine match against identified tracklets.
3. **Trajectory prior** — for OCR/gallery misses, constant-velocity prediction
   + roster-position prior + team-membership constraint → Hungarian assignment
   (`tracking.trajectory_prior_reid`, pure-NumPy Hungarian — `scipy` is not a
   worker dependency).
4. **Min-cost-flow stitching (nightly only)** — stitch fragmented tracklets of
   the same identity *within the clip* via a successive-shortest-path min-cost
   flow (`tracking.min_cost_flow_stitcher`, pure-Python — no OR-Tools/LPSolve),
   then propagate the `player_id` across each stitched group without
   overwriting an existing one.

The per-job artifact records the routed OCR variant under
`output_artifacts["model_routing"]["reid"]` and the per-layer detail under
`output_artifacts["reid_strategy"]`.

## Overriding routing

Set `MODEL_ROUTING_CONFIG` to the path of a JSON file shaped like
`gpu-worker/pipeline/model_routing.json`. Partial overrides are merged on
top of `DEFAULT_ROUTING`, so a config only has to name the stages it
changes.

```json
{
  "detect": {"same_session": "yolov8s", "nightly": "yolov8m"}
}
```

If the file is missing or malformed the worker logs a warning and falls
back to the defaults — it does not crash.

## Audit trail

Every completed job records the routing decision in
`processing_jobs.output_artifacts["model_routing"]` as a `{stage:
variant}` dict. Inspect it to confirm which variant served a clip:

```sql
SELECT id, job_type, priority, output_artifacts->'model_routing'
FROM processing_jobs
WHERE id = '...';
```

This is additive — existing artifact keys are untouched.

## Not routed: pre-snap prediction (Issues #135 / #136)

The pre-snap run/pass predictor (`gpu-worker/pipeline/play_prediction/`,
`stage_presnap_prediction.py`) consumes the **outputs** of the routed stages
and runs a small classifier + deterministic Bayesian math. In its default path
it loads no heavy weights and runs identically in both priority buckets, so —
exactly like the Bayesian snap detector — it registers **no** `model_router`
stage and is absent from `DEFAULT_ROUTING`. Optional formation-MLP / motion-LSTM
checkpoints (`PLAY_PREDICTION_FORMATION_MODEL` / `PLAY_PREDICTION_MOTION_MODEL`)
load at runtime and fall back to the deterministic path when absent. See
[`docs/pre-snap-prediction.md`](pre-snap-prediction.md).

## Not routed: tendency-break + frontier analytics (Issues #137 / #10)

The self-scout **tendency-break engine**
(`gpu-worker/pipeline/play_prediction/tendency_break_engine.py`,
`app/analytics/tendency_break.py`) and the **frontier analytics** scaffolds
(`gpu-worker/pipeline/frontier_analytics.py` — xSep / xYards / xPressure)
consume the **outputs** of the routed stages (labels, calibrated tracking) and
run pure deterministic math. They load no model weights and run identically in
both priority buckets, so — exactly like the pre-snap predictor and the
Bayesian snap detector — they register **no** `model_router` stage and are
absent from `DEFAULT_ROUTING`.

Frontier metrics are produced as **experimental** (`experimental_flag=True`,
`analytics_safe=False`) and source-labeled; the backend forces these metric
names experimental on ingest (`app.routers.clips.create_metric`) so an
unvalidated number can never be stored as trusted. SAM masks (#74) and play
embeddings (#8) are enrichment-only inputs — the metrics still compute when they
are absent, and no second SAM/embeddings path is introduced.

## Not routed: coverage GNN + pre-snap pressure (Issues #139 / #140 / #146)

The coverage classifier (`gpu-worker/pipeline/coverage/`, `stage_coverage.py`)
and the pre-snap pressure predictor (`gpu-worker/pipeline/pressure/`,
`stage_pressure.py`) consume the **outputs** of the routed stages (calibrated
tracking, snap anchor, OL/DL identification) and run pure-NumPy graph/heuristic
math over the shared spatial schema (`gpu-worker/pipeline/spatial/`). In their
default path they load **no heavy weights** and run identically in both priority
buckets, so — exactly like the pre-snap run/pass predictor and frontier
analytics — they register **no** `model_router` stage and are absent from
`DEFAULT_ROUTING`.

Optional offline-trained checkpoints (`COVERAGE_GNN_MODEL`, `PRESSURE_MODEL`) are
small CPU-friendly NumPy artifacts that load at runtime and **fall back** to the
deterministic baseline when absent — mirroring `PLAY_PREDICTION_FORMATION_MODEL`
/ `REID_OCR_MODEL`. They never touch `output_artifacts["model_routing"]`, so the
routing audit for the routed stages is preserved untouched. Both classifiers
ship a calibrated confidence (`CalibratedOutput`, Issue #146) and are flagged
**experimental / uncalibrated** until an offline-trained, Toledo-validated,
calibrated checkpoint is supplied; pressure metrics are additionally forced
`experimental_flag=True` / `analytics_safe=False` on write. A future heavy GNN
variant that needs the GPU must be added to `DEFAULT_ROUTING` +
`gpu-worker/tests/test_model_router.py` and benchmarked before any same-session
use (the "experimental → nightly" rule). See
[`docs/coverage-pressure-features.md`](coverage-pressure-features.md).

## Not routed: counterfactual coverage simulator (Issue #141)

The MVP counterfactual simulator
(`gpu-worker/pipeline/counterfactual/lookup_simulator.py`, and the backend port
`app/analytics/counterfactual.py` behind `POST /api/v1/counterfactuals`) is a
deterministic lookup + empirical-Bayes regression over historical
`(route_concept × coverage_type) → yards` observations. It loads **no model
weights** and runs identical pure-Python math in both priority buckets,
consuming the *outputs* of the routed stages — so, exactly like the pre-snap
predictor (#135/#136), frontier analytics (#10), and the coverage/pressure
classifiers (#139/#140), it registers **no** `model_router` stage and is absent
from `DEFAULT_ROUTING`. It does not touch `output_artifacts["model_routing"]`.

The V2 diffusion trajectory generator
(`gpu-worker/pipeline/counterfactual/diffusion_simulator.py`) is a **deferred,
inert scaffold** — it generates nothing and loads no weights. If it is ever
promoted to a GPU runtime path, it must be added to `DEFAULT_ROUTING` +
this doc + `gpu-worker/tests/test_model_router.py` and clear the
"experimental → nightly" bar first. See
[`docs/counterfactual-simulator.md`](counterfactual-simulator.md).

## Unknown stages

`select_model("not-a-stage", priority)` returns the module-level
`UNKNOWN_STAGE_FALLBACK` string (`"default"`) and logs a
`model_router_unknown_stage` warning. It does not raise, so a typo in a
job message never fails a pipeline outright.
