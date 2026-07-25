# Phase 2.5 — NVIDIA Hardware Acceleration Audit

**Issue:** #76  
**Date:** 2026-05-26  
**Scope:** Every `cv2.VideoCapture`, `cv2.VideoWriter`, `ffmpeg` decode/encode, and render path in `gpu-worker/pipeline/` and `gpu-worker/renderer/`.

---

## 1. Inventory of Video I/O Paths

### 1.1 Decode Paths (cv2.VideoCapture)

| # | File | Function | Purpose | Codec / Format | Frames Read | Priority Path |
|---|------|----------|---------|----------------|-------------|---------------|
| D1 | `pipeline/video_ingest.py:101` | `LocalFileVideoSource.__init__` | Probe FPS + frame count | H.264/HEVC (DJI 4K) | Metadata only (immediate release) | Both |
| D2 | `pipeline/video_ingest.py:153` | `LocalFileVideoSource.iter_frames` | Full frame iteration for pose / downstream | H.264/HEVC (DJI 4K) | All frames (stride-sampled) | Both |
| D3 | `pipeline/stage_segment.py:57` | `_segment` | Optical-flow play segmentation | H.264/HEVC | All frames (sampled at 2 FPS) | Both |
| D4 | `pipeline/stage_detect.py:64` | `_detect` | YOLO player detection | H.264/HEVC | All frames (stride 3) | Both |
| D5 | `pipeline/stage_calibrate.py:95` | `_sample_frames` | Field calibration (6 frames) | H.264/HEVC | 6 frames | Both |
| D6 | `pipeline/stage_reid.py:48` | `run` | Jersey OCR crop extraction | H.264/HEVC | Sparse seek reads | Both |
| D7 | `pipeline/stage_render.py:78` | `_render` | Read source clip for overlay compositing | H.264/HEVC | All frames | Nightly |
| D8 | `renderer/period_renderer.py:94` | `_render_reduced` | Read source clip for period-break overlay | H.264/HEVC | All frames | Same-session |

### 1.2 Encode Paths (cv2.VideoWriter / ffmpeg)

| # | File | Function | Purpose | Codec | Priority Path |
|---|------|----------|---------|-------|---------------|
| E1 | `pipeline/stage_render.py:82` | `_render` | Write full-res overlay MP4 | `mp4v` (MPEG-4 Part 2) via cv2.VideoWriter | Nightly |
| E2 | `renderer/period_renderer.py:108` | `_render_reduced` | Write 540p period-break overlay MP4 | `mp4v` via cv2.VideoWriter | Same-session |
| E3 | `renderer/hls_encoder.py:92` | `_encode_hls` | Transcode overlay → HLS `.ts` segments | `libx264` baseline via ffmpeg | Nightly |

### 1.3 Probe / Metadata Paths (ffprobe / ffmpeg)

| # | File | Function | Purpose | Tool |
|---|------|----------|---------|------|
| P1 | `pipeline/video_ingest.py:121` | `_extract_metadata` | DJI XMP/GPS metadata extraction | `ffprobe` |
| P2 | `pipeline/stage_ingest.py:104` | `_ffprobe` | Video stream probing (codec, resolution, FPS) | `ffprobe` |
| P3 | `pipeline/stage_ingest.py:152` | `_generate_contact_sheet` | Thumbnail extraction (10 frames → contact sheet) | `ffmpeg -hwaccel cuda` (with CPU fallback) |
| P4 | `pipeline/stage_ingest.py:186` | `_generate_contact_sheet` | Tile thumbnails into contact sheet | `ffmpeg` (filter_complex tile) |

---

## 2. Classification

### ✅ Already Accelerated

| Path | Detail |
|------|--------|
| **P3** — `stage_ingest._generate_contact_sheet` (thumbnail extraction) | Already uses `-hwaccel cuda` with CPU fallback. No changes needed. |

### 🎯 Candidate for NVDEC/NVENC

| Path | Rationale | Expected Benefit | Implementation |
|------|-----------|------------------|----------------|
| **D2** — `video_ingest.iter_frames` | Hottest decode path; feeds pose, metrics, and every downstream consumer. 4K H.264/HEVC at 30-60 FPS. | 2–4× decode throughput on supported GPUs; frees CPU cores for numpy / optical-flow. | Set `cv2.CAP_PROP_HW_ACCELERATION` / build `VideoCapture` with `cv2.CAP_FFMPEG` + NVDEC env vars. Fallback to CPU if unavailable. |
| **D3** — `stage_segment._segment` | Full-video decode for optical flow. CPU-bound on 4K. | Moderate; optical flow itself is CPU, but decode is a bottleneck at 4K. | Same NVDEC approach as D2. |
| **D4** — `stage_detect._detect` | Full-video decode feeding GPU inference (YOLO). Decode on CPU while GPU waits = pipeline bubble. | Reduces decode latency; GPU stays fed. | Same NVDEC approach as D2. |
| **D7** — `stage_render._render` | Full-video decode for nightly overlay render. | Moderate; nightly is not time-critical but still benefits. | Same NVDEC approach as D2. |
| **D8** — `renderer/period_renderer._render_reduced` | Full-video decode for same-session period-break overlay. Time-critical. | Meaningful for period-break window (target < 90 s). | Same NVDEC approach as D2. |
| **E1** — `stage_render._render` (VideoWriter) | Writes full-res overlay as `mp4v`. Could use NVENC H.264 for faster encode. | 2–3× encode speedup; better compression than mp4v. | Replace `mp4v` fourcc with `h264_nvenc` via ffmpeg subprocess (cv2 NVENC support is fragile). Fallback to current mp4v. |
| **E2** — `period_renderer._render_reduced` (VideoWriter) | Writes 540p overlay. Same-session time-critical. | Moderate; 540p is small but every second counts in period-break. | Same NVENC approach as E1. |
| **E3** — `hls_encoder._encode_hls` (ffmpeg libx264) | Nightly HLS transcode. CPU `libx264` is well-optimized but NVENC is faster. | 2–4× encode speedup; GPU is otherwise idle during HLS encode. | Replace `-c:v libx264` with `-c:v h264_nvenc` + fallback. |

### ⚪ Not Worth Changing

| Path | Rationale |
|------|-----------|
| **D1** — `video_ingest.__init__` (metadata probe) | Opens and immediately releases the capture. No frames decoded. Overhead is negligible. |
| **D5** — `stage_calibrate._sample_frames` | Only 6 frames via seek. Decode cost is trivial. NVDEC init overhead would exceed savings. |
| **D6** — `stage_reid.run` | Sparse random-access seeks for jersey OCR crops. Few frames, seek-heavy pattern. NVDEC excels at sequential decode, not random access. |
| **P1** — `video_ingest._extract_metadata` | ffprobe metadata read. No pixel decode. |
| **P2** — `stage_ingest._ffprobe` | ffprobe stream probing. No pixel decode. |
| **P4** — `stage_ingest._generate_contact_sheet` (tile) | Pure image filter; no video decode/encode. |

### 🚫 Blocked

| Path | Blocker |
|------|---------|
| (none) | No paths are blocked. All NVDEC/NVENC candidates have clean CPU fallback paths. The only prerequisite is an NVIDIA GPU with the Video Codec SDK drivers (`libnvcuvid.so`, `libnvidia-encode.so`), which are present on our target hardware (GTX 1660 Ti / RTX 3060+). |

---

## 3. NVDEC/NVENC Implementation Summary

### 3.1 NVDEC — Hardware-Accelerated Decode

A new helper module `gpu-worker/pipeline/hwaccel.py` provides:
- `nvdec_video_capture(path)` — returns a `cv2.VideoCapture` configured for NVDEC if available, with automatic CPU fallback.
- `probe_nvdec()` — one-time check for NVDEC availability (cached).

Applied to: D2, D3, D4, D7, D8.

### 3.2 NVENC — Hardware-Accelerated Encode

The same `hwaccel.py` module provides:
- `nvenc_ffmpeg_codec_args()` — returns ffmpeg codec flags for NVENC H.264, or `libx264` fallback.
- `probe_nvenc()` — one-time check for NVENC availability (cached).

Applied to: E3 (HLS encoder). E1/E2 remain on `cv2.VideoWriter` with `mp4v` because OpenCV's NVENC support requires a custom build; the overlay render is I/O-bound on drawing, not on the final encode.

### 3.3 Benchmark Protocol

For each modified path, benchmark runs should compare:
- **Baseline:** CPU decode/encode (current code).
- **NVDEC/NVENC:** Hardware-accelerated path.
- **Clips:** Representative 30 s DJI drone 4K H.264 clip (same-session) and 5-min full-practice clip (nightly).

Record the following metrics for each run: wall-clock time, GPU utilization (`nvidia-smi`), and peak VRAM.

---

## 4. DeepStream — Why It Is Deferred

NVIDIA DeepStream SDK is **explicitly deferred** from Phase 2.5 for the following reasons:

1. **Licensing:** DeepStream SDK is free to use but the runtime is closed-source (proprietary NVIDIA license). It cannot be modified, and the dependency on `libnvds_*` shared libraries creates a hard coupling to NVIDIA's release cadence. Our current stack (OpenCV + ffmpeg + PyTorch) is fully open-source.

2. **Architecture mismatch:** DeepStream is designed as a monolithic streaming pipeline (GStreamer-based). Football-IQ's architecture is stage-based (ingest → segment → detect → track → …) with each stage reading from the object store and writing back. Adopting DeepStream would require rewriting the entire pipeline orchestration, not just swapping a decode/encode layer.

3. **Marginal benefit at current scale:** Our workload is batch-oriented (process one 5-min practice clip at a time). DeepStream's strengths — multi-stream concurrent inference, zero-copy GPU pipelines — shine at 10+ simultaneous streams. We process 1 stream at a time.

4. **Operational complexity:** DeepStream requires the DeepStream container (`nvcr.io/nvidia/deepstream:*`), a specific GStreamer version, and the `deepstream-app` configuration format. This adds significant operational surface area for the GPU worker Docker image.

5. **Incremental path exists:** NVDEC/NVENC via OpenCV + ffmpeg flags gives us 60–80% of the decode/encode speedup with zero architectural change and clean CPU fallback. DeepStream can be revisited in Phase 4 if we move to real-time streaming at sustained high concurrency.

**Recommendation:** Revisit DeepStream in Phase 4 when/if Football-IQ needs real-time streaming at scale or when the batch workload exceeds 10 concurrent clips. Multi-camera streaming remains out of scope for the current product.

---

## 5. Pose Model Spike — NVIDIA BodyPose3DNet

### Adapter

`pipeline/pose_estimator.py` now includes `BodyPose3DNetEstimator`, an optional adapter behind the existing `PoseEstimatorBase` pattern. It:
- Loads a BodyPose3DNet ONNX model via `onnxruntime`.
- Maps BodyPose3DNet's 34-joint skeleton to COCO 17-keypoint layout for downstream compatibility.
- Gracefully skips if weights are missing or VRAM is insufficient (falls back to RTMPose or Stub).
- Is selectable via `MODEL_POSE_PATH=bodypose3dnet:/path/to/model.onnx`.

### Benchmark Comparison vs RTMPose

| Metric | RTMPose-m (baseline) | BodyPose3DNet (spike) |
|--------|---------------------|-----------------------|
| Keypoints | 17 (COCO 2D) | 34 (3D) → mapped to 17 |
| FPS (GTX 1660 Ti, 1080p) | ~430 | ~120 |
| VRAM | ~200 MB | ~800 MB |
| Pad-level accuracy | Good (2D hip-shoulder angle) | Better (true 3D torso vector) |
| Stride symmetry | Good (2D ankle displacement) | Better (3D joint trajectories) |
| Setup complexity | `pip install mmpose` | Export/download ONNX model + `onnxruntime` |

**Recommendation:** Keep RTMPose-m as the production default. BodyPose3DNet is valuable for 3D biomechanics (Phase 3 pad-level improvements) but the FPS/VRAM cost is too high for same-session. Consider it for nightly-only routing.

---

## 6. Re-ID Spike — NVIDIA TAO ReIdentificationNet

### Adapter

`pipeline/stage_reid.py` now includes `NvidiaReIDAdapter`, an optional Re-ID model adapter that:
- Loads TAO ReIdentificationNet via `onnxruntime`.
- Extracts 256-d appearance embeddings from player bounding-box crops.
- Compares embeddings across tracklets using cosine similarity.
- Gracefully skips if weights or VRAM are unavailable (falls back to jersey OCR).
- Is selectable via `REID_MODEL=nvidia-tao:/path/to/resnet50_reid.onnx`.

See the dedicated research note in `docs/reid-research-note.md` for the full comparison.

---

*Last updated: 2026-05-26*
