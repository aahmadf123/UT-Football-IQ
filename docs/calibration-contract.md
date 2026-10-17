# Field calibration contract (Issues #127, #138)

Calibration is the critical-path foundation that every spatial metric sits on
— route tree, formation, coverage, run/pass, self-scout, and distillation all
read the homography and its confidence. This document is the contract that
downstream stages (#128/#129/#132/#133/#135/#139/#140/#150) can rely on.

Calibration is **pixel-only** (no GPS/IMU/SRT — Toledo film is MP4-only) and
fits one camera view per calibration window. Edited game film supplies an
independent source window and capture regime for each generated angle.

## Coordinate system

The homography maps pixel coordinates to a standardized NCAA field frame:

- **X**: `0 → 100` yards (goal line to goal line, end zones excluded)
- **Y**: `-26.665 → +26.665` yards (south sideline → north sideline)
- Inbound hash marks at `±13.335` yd (40 ft from each sideline)
- Yard lines painted every 5 yards: `X ∈ {5, 10, …, 95}`

These NCAA-standard constants live in
`gpu-worker/pipeline/homography/field_template.py` as a `FieldTemplate`
dataclass. They are **not** Toledo-specific — a venue with non-standard
markings can supply an override without touching call sites.

## Capture regimes (Issue #126)

| Regime           | Film     | Calibration strategy |
| ---------------- | -------- | -------------------- |
| `fixed_sideline` | game     | Elevated, bolted-down camera. A full-recording homography may be flagged `is_game_anchor=True` and reused; edited game-film clips are calibrated independently per angle window. |
| `fixed_endzone`  | game     | Static end-zone camera. Fit a separate homography inside each generated angle window; never reuse the sideline transform. |
| `drone_follow`   | practice | Operator pans/zooms. Calibrated **per window**; the nightly variant Kalman-smooths the homography series and uses chained-ECC drift as the temporal-stability signal. |
| `unknown`        | fallback | Treated as `drone_follow` (the safer per-window path). |

## Shared math core

1. **White-paint detection** — HSV threshold (high V, low S) gated to the
   grass mask, morphological close to bridge dashed hashes.
2. **Hough lines** — `cv2.HoughLines` on Canny edges of the paint mask.
3. **Angle clustering** — group lines by orientation (~5° tolerance) into
   near-vertical (yard lines) and near-horizontal (sidelines/hashes).
4. **Correspondences** — match detected lines to the field template and emit
   labeled pixel↔yard intersections.
5. **Normalized DLT + RANSAC** — Hartley normalization (centroid→origin, RMS
   distance→√2), SVD solve of `Ah = 0`, RANSAC with a 3 px re-projection
   threshold.
6. **Confidence scoring** — five-component blend (below).

## Confidence components (persisted for downstream + UI)

```
confidence = 0.30 * inlier_ratio
           + 0.25 * min(line_count / 15, 1)
           + 0.20 * parallel_line_score
           + 0.15 * temporal_stability
           + 0.10 * field_coverage
```

All five sub-scores are written to
`field_calibrations.calibration_points["confidence_components"]` so the
clip-review UI can explain *why* a calibration is weak, not just its blended
score. A calibration is `analytics_safe` when `confidence ≥ 0.75` **and** no
disqualifying reason code is present.

## Persisted schema (migration 0017)

`field_calibrations` gains:

| Column              | Meaning |
| ------------------- | ------- |
| `kalman_state`      | 9-vector `vec(H)` from `drone_follow` nightly smoothing (JSON) |
| `inlier_ratio`      | RANSAC inlier ratio of the chosen fit |
| `line_count`        | field lines detected on the calibration frame |
| `parallel_variance` | angular variance of detected yard lines |
| `temporal_drift`    | mean inter-window re-projection drift in pixels (0.0 for a fixed anchor) |
| `is_game_anchor`    | the full-recording `fixed_sideline` homography reused across plays; angle-window calibrations are not anchors |

The existing `homography`, `confidence`, `confidence_threshold`,
`analytics_safe`, `reason_codes`, and `calibration_points` columns are
unchanged. All new columns are nullable (`is_game_anchor` defaults `FALSE`);
the migration upgrades and downgrades cleanly.

## Camera-motion compensation (Issue #138)

For `drone_follow`, full per-frame homography is expensive and noisy.
`gpu-worker/pipeline/homography/camera_motion_ecc.py` recovers inter-frame
motion from pixels alone:

1. **Anchor** every ~15 frames with a full calibration (`confidence > 0.7`).
2. **Inter-frame warp** via `cv2.findTransformECC` on the non-player
   background mask (grass + paint, players masked out by the detector).
3. **Chain** the warps onto the most recent anchor.
4. **Re-anchor** when chained-vs-direct drift exceeds 4 px or zoom magnitude
   exceeds 1.15× per second.
5. **`fixed_sideline` shortcut** — ECC returns identity; the chain collapses
   to a single H for the whole clip.

## Routing

The `calibrate` stage routes via `pipeline/model_router.py`:

- Same-session → `calib-hough-dlt` (light Hough + DLT, no Kalman).
- Nightly → `calib-hough-dlt-kalman` (adds Kalman temporal smoothing).

Both are pixel-only OpenCV/NumPy paths — no heavy model inference — so neither
is on the `NIGHTLY_ONLY_VARIANTS` guardrail. See
[model-routing.md](model-routing.md).

## Stage output artifacts

`stage_calibrate.run(...)` returns (and merges additively into the job's
`output_artifacts`):

```json
{
  "analytics_safe": true,
  "confidence": 0.86,
  "capture_regime": "fixed_sideline",
  "confidence_components": { "inlier_ratio": 1.0, "line_count_score": 0.93, "...": "..." },
  "is_game_anchor": true,
  "reason_codes": []
}
```

## Validation status

Unit tests cover both regimes, the DLT/RANSAC core on synthetic
correspondences, the Kalman smoother over a 100-frame sequence, ECC warp
recovery, and the `stage_calibrate` regime branches
(`gpu-worker/tests/test_calibrate_yardline.py`, `test_dlt_ransac.py`,
`test_kalman_homography.py`, `test_camera_motion_ecc.py`).

The data-dependent acceptance gates from Issue #127 — mean re-projection
error `< 3 px` on labeled fiducial frames and `≥ 80%` of Toledo clips at
`analytics_safe` with `confidence ≥ 0.75` — require labeled Toledo film and
are validated separately once that evaluation set is available. The frontend
clip-review surface for the confidence sub-components is a deferred follow-up
(the data is exposed via `GET /api/v1/videos/{id}/calibrations`).
