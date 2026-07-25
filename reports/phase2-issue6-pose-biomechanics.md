# Phase 2 — Issue #6: Pose-Lite Biomechanics Pilot

**Status:** Implemented  
**Branch:** `claude/football-iq-phase-2-TagEt`  
**Date:** 2026-05-08

---

## Summary

This report documents the implementation of the Pose-Lite Biomechanics Pilot (Issue #6), the final
component of Phase 2.  All work is self-contained behind an `experimental_flag=True` governance
gate: no pose metrics appear in any dashboard until a position coach explicitly approves them via
the review API.

---

## Files Added or Modified

### New — GPU Worker Pipeline

| File | Purpose |
|------|---------|
| `gpu-worker/pipeline/video_ingest.py` | Pluggable video source protocol (local .mp4, R2, mock) |
| `gpu-worker/pipeline/pose_estimator.py` | RTMPose-m adapter + deterministic stub; geometry helpers |
| `gpu-worker/pipeline/stage_pose.py` | Core biomechanics stage — OL/DL, WR, QB, all-player metrics |
| `gpu-worker/tests/test_stage_pose.py` | 42 unit tests for pipeline logic |

### Modified — GPU Worker

| File | Change |
|------|--------|
| `gpu-worker/__main__.py` | Replaced `pose` dispatch stub with real `stage_pose.run()` call |
| `gpu-worker/pipeline/backend.py` | Added `create_pose_keypoints()` and extended `create_metric()` with `tracklet_id`, `experimental_flag`, `analytics_safe`, `confidence` params |

### New — Backend

| File | Purpose |
|------|---------|
| `backend/app/routers/pose.py` | 6 pose-specific REST endpoints |
| `backend/migrations/versions/0006_pose_biomechanics.py` | Adds `biomechanics` JSONB column + `pose_biomechanics_tag` enum value |
| `backend/tests/test_pose.py` | 22 unit/endpoint tests |

### Modified — Backend

| File | Change |
|------|--------|
| `backend/app/models.py` | Added `biomechanics: JSON` field to `PoseKeypoints`; added `pose_biomechanics_tag` to `CorrectionType` enum |
| `backend/app/deps.py` | Added `require_sportsperformance_or_above` role guard (admin + analyst + sportsperformance) |
| `backend/app/main.py` | Registered `pose_router` |

---

## Architecture

### Video Ingestion Layer (`video_ingest.py`)

DJI drone .mp4 readiness layer. All downstream pipeline code accepts a `VideoSource` — never a raw
path — so the data source can be swapped without changing the biomechanics logic.

```
VideoSource (Protocol)
  .iter_frames(stride=1) → Iterator[tuple[int, ndarray]]
  .fps: float
  .total_frames: int
  .metadata: dict          ← DJI GPS / altitude from ffprobe (graceful no-op if absent)

LocalFileVideoSource(path)   ← Real .mp4 via cv2.VideoCapture
ObjectStoreVideoSource(object_key)        ← Downloads from the object store to a temp file
MockVideoSource(...)         ← Deterministic synthetic frames (CI / unit tests)
open_video(uri) ctxmgr       ← Routes: None → Mock, s3://… → R2, else → Local
```

### Pose Estimation Layer (`pose_estimator.py`)

Self-configuring: set `MODEL_POSE_PATH` to activate real RTMPose-m inference; leave it unset for
stub mode in CI.

```
RTMPoseEstimator(model_path)   ← MMPose inference — 430 FPS on GTX 1660 Ti
StubPoseEstimator(seed=42)     ← Deterministic 17-keypoint COCO layout, no GPU required
get_estimator(model_path)      ← Factory: real if mmpose importable, else stub + warning
```

Keypoint schema: 17-point COCO (nose, eyes, ears, shoulders, elbows, wrists, hips, knees, ankles).
All downstream code uses this schema regardless of whether RTMPose or the stub ran.

Geometry helpers (`angle_degrees`, `vector_angle_from_vertical`, `midpoint`, `kp`) are pure
functions with no model or frame dependency.

---

## Biomechanics Metrics

All 14 metrics are written with `experimental_flag=True, analytics_safe=False`.

### OL / DL — Pad Level & Leverage

| Metric | Description |
|--------|-------------|
| `pose_pad_level` | Mean hip-flexion angle + torso lean over a clip. Low torso angle = upright (pass-set); high = forward drive. |
| `pose_pass_set_weight_distribution` | Classifies intent: `run_block` (torso > 20°) vs `pass_set`. Includes `forward_lean_score` 0–1. |
| `pose_block_shed_timing` | Seconds from contact event until DL torso returns to < 15° (upright = shed). |

**Implementation:** hip→knee→ankle chain angle (hip flexion) + hip-centre→shoulder-centre vector vs. vertical (torso angle). Frames below 0.3 confidence are skipped.

### WR — Breakpoint Mechanics

| Metric | Description |
|--------|-------------|
| `pose_wr_pre_snap_stance` | `80_20` vs `50_50` foot split from ankle x-offset ratio. |
| `pose_wr_shoulder_over_knee` | Shoulder-centre x vs knee-centre x at route top; `overextended=True` slows COD. |
| `pose_wr_deceleration_steps` | Local ankle-y maxima before route break. `elite=True` for 2–3 steps. |
| `pose_wr_hip_sink_depth` | Vertical CoM drop in yards + velocity (yps) into the break. |
| `pose_wr_release_technique` | Wrist motion classification: `club_rip`, `club_swim`, `gangster`, `speed`, `unknown`. |

**Implementation:** Route top detected as max hip-x displacement across the clip. Pre-snap window = first frame; break window = 0.5 s before route top.

### QB — Mechanics

| Metric | Description |
|--------|-------------|
| `pose_qb_stride_consistency` | Mean + std of stride length (yards) and knee flexion during dropback. |
| `pose_qb_shoulder_hip_separation` | Max shoulder-hip separation angle + trunk rotation velocity (rad/s). |
| `pose_qb_weight_transfer` | Back-to-front CoM shift score 0–1 during dropback. |
| `pose_qb_release_consistency` | Arm angle mean + std around throw frame (requires `throw` event). |

**Implementation:** Dropback sequence starts at `snap` event frame. Separation uses shoulder-vector vs. hip-vector angle. Weight transfer normalises CoM shift against 80 px reference.

### All Players — Gait & Drift

| Metric | Description |
|--------|-------------|
| `pose_stride_symmetry` | Left/right stride ratio + asymmetry score. `injury_risk_flag=True` when score > 0.15. Feeds UT Athletics model. |
| `pose_biomechanical_drift` | Torso angle trend across OL tracklets in a clip: `stable`, `declining`, `improving`. Computed when ≥ 2 OL tracklets present. |

---

## API Endpoints (`/api/v1/pose`)

| Method | Path | Role | Description |
|--------|------|------|-------------|
| `POST` | `/keypoints` | coach+ | Store raw keypoints (called by GPU worker) |
| `GET` | `/keypoints` | non-player | List keypoints by `tracklet_id` |
| `GET` | `/keypoints/{id}` | non-player | Single keypoints row |
| `GET` | `/metrics/pending` | coach+ | Experimental pose metrics awaiting review; filter by `position_group` |
| `POST` | `/metrics/{id}/review` | coach+ | Approve / reject / flag a pose metric |
| `GET` | `/asymmetry` | sportsperformance+ | Stride symmetry trends |

Player-facing views are **completely blocked** from all pose endpoints regardless of `analytics_safe` status. The sportsperformance-only asymmetry endpoint is restricted because raw biomechanical risk data requires clinical context before player exposure.

---

## Governance Flow

```
GPU Worker writes metric
  → experimental_flag=True, analytics_safe=False
  → hidden from all dashboards

Position coach reviews via POST /pose/metrics/{id}/review
  → review_action="approve"
  → metric.analytics_safe=True
  → metric visible in staff dashboards (not player-facing)

review_action="reject" → metric stays suppressed
review_action="flag"   → routed to model review queue
```

Reviews are stored in the existing `head_orientation_reviews` table (reused from Phase 1 head-orientation work). No new approval tables were needed.

---

## Database Changes (Migration 0006)

```sql
-- Extend correction_type enum
ALTER TYPE correction_type ADD VALUE IF NOT EXISTS 'pose_biomechanics_tag';

-- Add biomechanics JSONB to pose_keypoints
ALTER TABLE pose_keypoints ADD COLUMN biomechanics JSON;
```

The `biomechanics` column stores per-frame computed angles (hip_flexion_degrees, torso_angle_degrees, stride_phase) alongside raw keypoints for efficient overlay queries without recomputing.

Migration chains from `0005` (Phase 2 football tables) → `0006`.

---

## DJI Drone .mp4 Readiness

No .mp4 footage was available during development. The system is ready for it:

1. `LocalFileVideoSource(path)` opens any .mp4 via `cv2.VideoCapture`.
2. DJI GPS / altitude metadata is extracted via `ffprobe` JSON parsing; absent `ffprobe` falls back to `{}` gracefully.
3. `open_video("s3://bucket/key.mp4")` downloads to a temp file and cleans up on exit.
4. `MODEL_POSE_PATH` env var activates RTMPose-m; unset → stub (zero config for CI).

When drone footage is available: set `MODEL_POSE_PATH=/weights/rtmpose-m.pth` and `BACKEND_API_URL`, then submit a `pose` job with `input_uri=s3://...`.

---

## Test Coverage

**GPU Worker** — `gpu-worker/tests/test_stage_pose.py` (42 tests, 0 failures):
- Geometry helpers: angle_degrees, vector_angle_from_vertical, midpoint, kp lookup
- StubPoseEstimator: determinism, seed variation, 17-keypoint COCO schema, confidence range
- MockVideoSource: frame count, stride, frame numbers, numpy dtype, empty source
- OL/DL: pad_level returns metric, hip_flexion positive, low-confidence skipped, pass-set classification, block-shed timing
- WR: shoulder-over-knee overextension, deceleration steps (elite=3, non-elite=1), pre-snap stance
- QB: shoulder-hip separation, too-short returns None
- Stride symmetry: symmetric flag=False, asymmetric flag=True, too-short returns None
- Integration: run() with empty tracklets, OL tracklet writes metrics with experimental_flag=True, all metric names pose_prefixed, no-frames returns zeros

**Backend** — `backend/tests/test_pose.py` (22 tests, 0 failures):
- Schema validation: POSE_METRIC_NAMES set, position-group filtering, Pydantic field guards
- Endpoint RBAC: player blocked, coach allowed for keypoints/pending, sportsperformance required for asymmetry
- Endpoint behaviour: keypoints list returns data, pending metrics returns experimental, approve sets analytics_safe=True

Total: **48 backend tests pass** (22 new + 26 existing). **42 gpu-worker tests pass**.

---

## Known Limitations and Next Steps

1. **No real footage tested.** All metrics use `StubPoseEstimator`. When RTMPose weights arrive (`MODEL_POSE_PATH`), the estimation layer is a drop-in swap.
2. **Single-player assumption.** The `RTMPoseEstimator` picks the highest-confidence detected person per frame. Multi-person tracking would need tracklet-to-detection assignment logic.
3. **Pixel-to-yard conversion.** The field-width approximation (1280 px ≈ 13.3 yd) should be replaced with the homography matrix from the calibration stage once `analytics_safe=True` calibrations are available.
4. **Biomechanical drift** requires ≥ 2 OL tracklets and ≥ 6 sampled frames to produce a metric. Sparse tracklets will not produce a drift metric.
5. **Player-id filtering** on the asymmetry endpoint is a query-param stub — the Tracklet model does not yet have a `player_id` FK. This will need to be wired up when the player profile model is added.
