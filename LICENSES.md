# LICENSES.md

Third-party models, libraries, and tools used in Football-IQ. Updated October
2026 for the local CPU worker and native distribution. Historical model spikes
below are not a statement that those weights are installed, validated, or bundled.

## Local CPU runtime and native packaging

The default worker uses `gpu-worker/requirements-cpu.txt`, with separate backend
and worker environments. Native packaging takes explicit PostgreSQL/pgvector and
FFmpeg binaries built for the target OS; it does not establish one universal
license for every possible binary input. Preserve the actual distribution's
copyright/license texts, third-party notices, and required source/relinking
materials. This inventory does not replace those texts in a redistributed bundle.

| Component | License / canonical source | Football-IQ use and distribution notes |
|---|---|---|
| **CPython** | [PSF License](https://docs.python.org/3/license.html) and included third-party licenses | Separate API/worker runtimes; development recommendations are Python 3.12/3.11. Native bundles include their actual interpreter. Preserve the notices from the exact Python build. |
| **NumPy 1.26.4** | [BSD-3-Clause](https://github.com/numpy/numpy/blob/v1.26.4/LICENSE.txt) for NumPy; wheel-specific third-party licenses | CPU numerical features and the local coverage trainer. Linux wheels include OpenBLAS/LAPACK notices and GCC runtime libraries (GCC Runtime Library Exception; libquadmath LGPL). Preserve the complete installed wheel license inventory rather than calling every bundled library BSD. |
| **SciPy 1.14.1** | [BSD-3-Clause](https://github.com/scipy/scipy/blob/v1.14.1/LICENSE.txt), plus wheel-specific bundled library licenses | Ultralytics tracking dependencies and numerical routines; pinned with NumPy 1.26.4 so tracker imports remain compatible. Preserve the installed wheel's complete third-party inventory, including BLAS/LAPACK/runtime notices. |
| **OpenCV / opencv-python and opencv-python-headless 4.10.0.84** | [OpenCV Apache-2.0](https://github.com/opencv/opencv/blob/4.10.0/LICENSE); [Python-wheel build sources](https://github.com/opencv/opencv-python) include third-party notices | Local decode, image features, and temporary video encoding. Preserve `LICENSE.txt` and `LICENSE-3RD-PARTY.txt` from the selected wheels, including their bundled FFmpeg/other libraries. The separately supplied FFmpeg CLI has its own build terms. |
| **PyTorch / torchvision** | [PyTorch BSD-3-Clause](https://github.com/pytorch/pytorch/blob/v2.8.0/LICENSE); [torchvision BSD-3-Clause](https://github.com/pytorch/vision/blob/v0.23.0/LICENSE), plus bundled third-party notices | Real local inference. Windows/Linux CPU and Apple Silicon pins are Torch 2.8.0/vision 0.23.0; Intel Mac has no Torch >= 2.6 wheels and uses the Linux CPU container. CUDA builds are optional. Preserve exact wheel notices; no model weights are committed. |
| **PostgreSQL 16** | [PostgreSQL License](https://www.postgresql.org/about/licence/), permissive | Native local database process, or an explicitly configured existing server. Preserve `COPYRIGHT` and notices for the actual PostgreSQL build and its shared-library dependencies. Server credentials/data remain private runtime files. |
| **pgvector** | [PostgreSQL License](https://github.com/pgvector/pgvector/blob/master/LICENSE), permissive | Extension compiled against the packaged PostgreSQL server; existing relational store and vector index. Preserve the extension license; no additional vector service. |
| **FFmpeg / ffprobe** | [LGPL-2.1-or-later by default; GPL when configured with GPL components](https://ffmpeg.org/legal.html) | Actual PTS probing, browser-playable H264 media, optional HLS. `libx264` builds are GPL, not LGPL-only. `--enable-nonfree` builds are not redistributable under FFmpeg's terms. Record the exact binary version/configuration and provide the applicable notices/corresponding source for redistribution. Build inputs are configurable; the Linux verification binary is GPL-enabled. |
| **libx264** | [GPL-2.0-or-later](https://code.videolan.org/videolan/x264) or a separately obtained commercial license | CPU H264 encoding in the supplied FFmpeg build. The default renderer invokes the external FFmpeg executable; preserve the encoder/build licensing and source obligations of that executable. |
| **PyInstaller 6.16.0** | [GPL-2.0-or-later with the PyInstaller bootloader exception](https://github.com/pyinstaller/pyinstaller/blob/v6.16.0/COPYING.txt) | Build-only dependency in `requirements-native-build.txt`; staff do not install PyInstaller. The bootloader exception permits distributing bundled applications under their own terms and does not waive the licenses of included Python/native dependencies. Preserve the applicable exception/license notices in the native distribution. |

None of these infrastructure libraries contains football training labels. Human
annotation snapshots, private film, and learned candidate checkpoints remain in
the configured local/object-store artifacts; they are not committed to Git.
Multiple independent recordings per session are supported. Optional adapters
still operate on one recording's frames and do not establish cross-view identity
or automatic synchronized 3D reconstruction.

---

## EasyOCR (scoreboard cards in game film)

| Field | Detail |
|---|---|
| **Package** | `easyocr==1.7.2` (JaidedAI) |
| **Code license** | [Apache-2.0](https://github.com/JaidedAI/EasyOCR/blob/master/LICENSE) |
| **Models** | Text detection (CRAFT, MIT per [clovaai/CRAFT-pytorch](https://github.com/clovaai/CRAFT-pytorch)) and English recognition weights, downloaded by the package on first use into the worker user's `~/.EasyOCR`; not committed or redistributed. |
| **Football-IQ usage** | `gpu-worker/pipeline/game_film.py` reads the stadium scoreboard card shown before each play in edited game film (quarter, clock, down & distance, ball-on). Optional: without it, plays are still segmented and only the scoreboard context is missing. |
| **Runtime category** | Staff GPU laptop worker only; never in the browser or the hosted API image. |
| **Data** | Reads Toledo's own film frames locally; nothing is sent to a third party. |

## Meta SAM 3 / SAM 3.1

| Field | Detail |
|---|---|
| **Model** | Segment Anything Model 3 (SAM 3) and SAM 3.1 |
| **Owner** | Meta AI |
| **Code license** | Meta open-source license (GitHub: `facebookresearch/sam3`) |
| **Weight license** | Gated access — Meta SAM Model License (Llama-family variant) |
| **Access** | Request at https://huggingface.co/facebook/sam3 — account approval required |
| **Commercial use** | Non-commercial / research use only per model card; review before any commercial deployment |
| **Integration path** | `pip install -U ultralytics` — SAM 3 is included in Ultralytics >= 8.3.237 |
| **HF token required** | Yes — `HF_TOKEN` env var / GitHub Actions secret |
| **Football-IQ usage** | Phase 2.5 — Issue #74: shipped as `SAM3Detector` in `pipeline/detector_models.py` and `SAM3MaskTracker` in `pipeline/tracker_models.py`. Routed via `model_router` on the nightly path only (`ENABLE_SAM3_NIGHTLY=1`); same-session continues to use YOLOv8n + IoU. Listed in `NIGHTLY_ONLY_VARIANTS` so config overrides cannot route it to a same-session bucket. |
| **Eval** | See `reports/phase2-issue74-sam3-eval.md` and `gpu-worker/eval/eval_sam3_vs_yolo.py` for the comparison harness (synthetic CI path + real-clip path). |
| **Promotion gate** | Frame coverage within 2pp of YOLOv8n, mean track length ≥ YOLO + IoU, fragmentation ≤ 1.1×, and same-session latency fit. Until met SAM 3.1 stays nightly-only; promotion likely awaits a distilled variant. |
| **Notes** | Do not commit model weights (`.pt`, `.pth`, `.safetensors`) to the repository — `.gitignore` enforces this. Weights are downloaded at runtime via `HF_TOKEN`. The adapter logs a warning at construction time when `HF_TOKEN` is unset so the failure mode is obvious. |

---

## NVIDIA TAO Toolkit — PeopleNet, BodyPose3DNet, ReIdentificationNet

| Field | Detail |
|---|---|
| **Toolkit** | NVIDIA TAO Toolkit (Train, Adapt, Optimize) |
| **Code license** | Apache 2.0 (open source as of TAO 5.0) |
| **Pretrained weight license** | NVIDIA Open Model License — free for use, **weights may not be redistributed or resold** |
| **BodyPose3DNet license** | CC BY 4.0 — cleanest IP path |
| **Access** | https://ngc.nvidia.com — NGC account + API key required |
| **NGC API key** | `NGC_API_KEY` env var / GitHub Actions secret |
| **Commercial use** | Models trained using NVIDIA pretrained weights as a starting point are owned by the user and commercially usable; the NVIDIA pretrained weights themselves cannot be resold |
| **Football-IQ usage** | Phase 2.5 / Phase 3 — Issue 76: hardware-accelerated decode (NVDEC), optional TAO ReIdentificationNet adapter for `stagereid.py`, optional BodyPose3DNet adapter for pose |
| **Models in scope** | PeopleNet (person detection), BodyPose3DNet (3D pose, CC BY 4.0), ReIdentificationNet (cross-camera re-ID) |
| **Deferred** | TAO PeopleNet fine-tune, DeepStream SDK (closed-source / enterprise), Triton Inference Server (medium-term), Cosmos World Models (long-term data augmentation) |
| **Notes** | Model weights are downloaded at runtime via `ngc` CLI or Docker from `nvcr.io`. Do not commit NVIDIA model artifacts to the repository. |

---

## NVIDIA Cosmos World Foundation Models

| Field | Detail |
|---|---|
| **Code license** | Apache 2.0 |
| **Weight license** | NVIDIA Open Model License |
| **Access** | https://ngc.nvidia.com |
| **Football-IQ usage** | Long-term / Phase 4 only — synthetic training data generation for rare-situation footage (low light, crowded box). Out-of-band data augmentation, not in the inference pipeline. |
| **Notes** | Deferred — requires 24.5 GB VRAM (7B) or 80 GB (14B). Cloud GPU (A100/H100) required. |

---

## sportsbd

| Field | Detail |
|---|---|
| **Library** | `sportsbd` by mehdih7 |
| **License** | MIT |
| **Access** | `pip install sportsbd` — no account required |
| **Football-IQ usage** | Issue 75 spike only — benchmark against current optical-flow play segmenter. Not promoted to production without benchmark evidence. |
| **Notes** | 2-star single-author library (as of May 2026). Designed for broadcast shot-boundary detection; applicability to continuous drone footage must be validated before any production use. Benchmark against PySceneDetect as an alternative. |

---

## SportsDataverse sportypy

| Field | Detail |
|---|---|
| **Library** | `sportypy` by SportsDataverse |
| **License** | GPL-3.0 |
| **Access** | https://sportypy.sportsdataverse.org and https://github.com/sportsdataverse/sportypy - `pip install sportypy`; no account required |
| **Sport coverage** | Multi-sport playing-surface renderer; includes American football NCAA/NFL fields. Avoid broad multi-sport adoption paths that could pull in soccer/association-football use. |
| **Football-IQ usage** | Issue #169 evaluation only. Recommended for optional analyst/report-only exploration, not as a production backend, frontend, Worker, or GPU-worker dependency. |
| **Secret/key required** | No |
| **Privacy risk** | Low when used with synthetic or approved aggregated field coordinates. Real route/spacing charts must wait for calibrated tracking gates and must not expose private footage or player PII. |
| **Model-router impact** | None - visualization only; no inference stage or model registry path. |
| **Notes** | GPL-3.0 requires legal review before any deployed-service dependency. No package dependency was added in the issue #169 PR. |

---

## SportsDataverse cfbplotR

| Field | Detail |
|---|---|
| **Library** | `cfbplotR` by SportsDataverse |
| **License** | MIT for package code; college football data/logos belong to their respective owners and are governed by their terms of use |
| **Access** | https://cfbplotr.sportsdataverse.org and https://github.com/sportsdataverse/cfbplotR - R/GitHub install; no account required |
| **Sport coverage** | College football visualization helpers, especially logo plotting in ggplot2 |
| **Football-IQ usage** | Issue #169 evaluation only. Deferred for production because Football-IQ should not add R to the backend for logo/chart rendering, and Toledo/MAC marks require explicit rights review. |
| **Secret/key required** | No package secret; this evaluation makes no CFBD or Sportradar calls |
| **Privacy risk** | Medium for logo/trademark handling; low for synthetic chart geometry |
| **Model-router impact** | None - visualization only; no inference stage or model registry path. |
| **Notes** | Use only approved local Toledo brand assets/tokens if frontend identity is needed. Do not expose vendor keys or fetch external logo/data catalogs in browser code. |

---

## SportsDataverse sportyR

| Field | Detail |
|---|---|
| **Library** | `sportyR` by SportsDataverse |
| **License** | GPL-3.0 |
| **Access** | https://sportyr.sportsdataverse.org and https://github.com/sportsdataverse/sportyR - R package; no account required |
| **Sport coverage** | Multi-sport playing-surface renderer; includes American football but also soccer surfaces |
| **Football-IQ usage** | Issue #169 evaluation only. Deferred; it duplicates the playing-surface use case while adding an R runtime and GPL-3.0 production-review burden. |
| **Secret/key required** | No |
| **Privacy risk** | Low when limited to synthetic or approved aggregated coordinates |
| **Model-router impact** | None - visualization only; no inference stage or model registry path. |
| **Notes** | Do not use for production Football-IQ rendering unless a future ADR explicitly approves R/GPL dependency handling. |

---

## Ultralytics (YOLOv8 / YOLOv11)

| Field | Detail |
|---|---|
| **License** | [AGPL-3.0](https://github.com/ultralytics/ultralytics/blob/v8.3.237/LICENSE) for the open-source distribution; [Ultralytics licensing options](https://www.ultralytics.com/license) include a commercial Enterprise license |
| **Commercial use** | AGPL permits commercial use subject to its source/distribution/network obligations. An Enterprise license is an alternative for use outside those obligations; commercial use alone is not an unconditional requirement to purchase one. Check the actual code and model-weight terms for the distribution. |
| **Access** | Pinned `ultralytics==8.3.237`; player weights explicitly installed/downloaded to the model directory, never committed or included as team-trained evidence. The hosted image pins the published `v8.3.0/yolov8n.pt` asset to SHA256 `f59b3d833e2ff32e194b5bb8e08d211dc7c5bdf144b90d2c8412c47ccfc83b36` |
| **Football-IQ usage** | CPU baseline player detector in `pipeline/detector_models.py`/`pipeline/stage_detect.py` — YOLOv8n COCO **person class 0**. Generic sports-ball classes are not used as a dedicated football model. SAM remains optional and is not installed with weights by the CPU workflow. |
| **Readiness** | Generic pretrained person detection is real inference, not independently validated Toledo analytics. Missing player weights make Quick analysis unavailable/partial; Review remains useful. Preserve AGPL and selected model notices in distributions. |

---

## PyTorch

| Field | Detail |
|---|---|
| **License** | BSD-3-Clause |
| **Access** | Platform-pinned `gpu-worker/requirements-cpu.txt`; default Docker base is `python:3.11-slim`, with explicit optional CUDA build arguments. See the native/CPU inventory above for wheel and third-party notices. |

---

## NVIDIA Video Codec SDK (NVDEC / NVENC)

| Field | Detail |
|---|---|
| **Component** | NVIDIA Video Codec SDK — hardware-accelerated video decode (NVDEC) and encode (NVENC) |
| **License** | [NVIDIA Video Codec SDK License Agreement](https://developer.nvidia.com/nvidia-video-codec-sdk-license-terms) |
| **Access** | Bundled with NVIDIA GPU drivers (≥ 470.x); no separate download required for runtime use. SDK headers available at https://developer.nvidia.com/video-codec-sdk |
| **Commercial use** | Yes — freely usable in commercial products |
| **Football-IQ usage** | Phase 2.5 — Issue #76: `pipeline/hwaccel.py` provides NVDEC-accelerated `cv2.VideoCapture` and NVENC-accelerated ffmpeg encode for `renderer/hls_encoder.py`. Transparent CPU fallback when GPU is unavailable. |
| **Notes** | NVDEC/NVENC capabilities are accessed through OpenCV's FFmpeg backend and the `ffmpeg` CLI (`h264_nvenc`). No NVIDIA SDK headers are compiled into Football-IQ. The driver-level codec libraries (`libnvcuvid.so`, `libnvidia-encode.so`) are part of the standard NVIDIA driver installation. |

---

## ONNX Runtime (GPU)

| Field | Detail |
|---|---|
| **Library** | `onnxruntime-gpu` by Microsoft |
| **License** | MIT |
| **Access** | `pip install onnxruntime-gpu` — no account required |
| **Football-IQ usage** | Phase 2.5 — Issue #76: optional runtime for NVIDIA TAO BodyPose3DNet and ReIdentificationNet ONNX models in `pipeline/pose_estimator.py` and `pipeline/stage_reid.py`. Only loaded when the corresponding model is configured. |

---

## College Football Data (CFBD)

| Field | Detail |
|---|---|
| **Resource** | College Football Data (CFBD) API |
| **Sport coverage** | College football ✅ (American football — Toledo Rockets / MAC). Not soccer. |
| **Toledo / MAC relevance** | Direct — Toledo + MAC schedules, games, drives, plays, team game stats, win probability. |
| **Source URL** | https://collegefootballdata.com — API https://api.collegefootballdata.com (org: https://github.com/CFBD; ecosystem: https://cfbfastr.sportsdataverse.org) |
| **License / access terms** | Free tier / API-key access; review CFBD terms and rate limits before any external or commercial deployment. Attribution to CollegeFootballData.com is surfaced in the UI. |
| **Runtime category** | Production API (backend-only) → cached ingestion into Postgres → read-only backend API. No live vendor call in the request path. |
| **Secret / key requirement** | `CFBD_API_KEY` (+ `CFBD_BASE_URL`) — backend environment variable / CI secret. Never exposed to frontend, browser bundles, logs, or coach-visible errors, and never stored in the database. |
| **Data privacy risk** | None expected — public team/game statistics. No PII, medical, or recruiting data ingested in v1. |
| **Model-router / registry path** | N/A — data integration, not an inference model. |
| **Overlap with closed decisions** | None. Single-camera (#101), pgvector (#8/#77), SAM (#74) decisions are untouched. |
| **Calibrated-tracking dependency** | None. |
| **Football-IQ usage** | Issues #160/#161/#162/#163 — backend `app/cfbd/` client + `cfbd_*` Postgres cache tables (migration 0016), plus read-only `/api/cfbd/*` and College Data frontend surfaces. Synced via `python -m app.cfbd --season <year>`. |
| **Notes** | No vendor key is committed, logged, returned to clients, or written to the database. Cached rows remain available when CFBD is unavailable. |

---

## Sportradar NCAAFB API v7 — evaluated, not adopted (Issue #165)

| Field | Detail |
|---|---|
| **Resource** | Sportradar NCAAFB (NCAA Football) API v7 |
| **Sport coverage** | American / college football ✅ (NCAA FB). Not soccer. |
| **Toledo / MAC relevance** | Broad college football incl. MAC; no Toledo-specific advantage over CFBD established. |
| **Source URL** | https://developer.sportradar.com/football/docs/ncaafb-ig-api-basics |
| **License / access terms** | Commercial B2B contract. Trial: 30 days / 1,000 calls / 1 QPS. Production QPS per signed package. Not redistributable; respect documented TTLs (2 s live PBP, 120 s seasonal stats). |
| **Runtime category** | Documentation only — **evaluated, not adopted** (spike #165). Would be backend-only production API if adopted. |
| **Secret / key requirement** | If adopted: proposed `SPORTRADAR_API_KEY` (+ `SPORTRADAR_BASE_URL`, `SPORTRADAR_ACCESS_LEVEL`, `SPORTRADAR_NCAAFB_VERSION`). Backend-only; `x-api-key` header; never exposed to frontend, browser bundles, logs, PR/issue text, stored artifacts, coach-visible errors, or the database. **No value committed.** |
| **Data privacy risk** | Public team/game statistics and game-day player availability statuses. No medical/wellness data; treat statuses as not-for-logging. |
| **Model-router / registry path** | N/A — data integration, not an inference model. |
| **Overlap with closed decisions** | None. CFBD (#160–#163) remains the authoritative college-data source; this spike does **not** replace it. Single-camera (#101), pgvector (#8/#77), SAM (#74) untouched. |
| **Calibrated-tracking dependency** | None (#127/#128/#129 not implicated). |
| **Decision** | **Not adopted now — defer** behind a scoped live in-game feature. CFBD stays authoritative. See [`reports/spike-issue165-sportradar-ncaafb-v7.md`](reports/spike-issue165-sportradar-ncaafb-v7.md). |

---

## NFL Big Data Bowl (BDB) — offline dataset adapter (Issue #164)

| Field | Detail |
|---|---|
| **Resource** | NFL Big Data Bowl tracking datasets (Kaggle competitions) |
| **Sport coverage** | NFL / American football ✅ (player tracking). Not soccer. |
| **Toledo / MAC relevance** | Broad American football. **Not** Toledo film and **not** Toledo labels — offline analogue only. |
| **Source URL** | Overview https://operations.nfl.com/gameday/analytics/big-data-bowl/ · BDB 2025 https://www.kaggle.com/competitions/nfl-big-data-bowl-2025 · BDB 2026 https://www.kaggle.com/competitions/nfl-big-data-bowl-2026-prediction · refs: formation https://operations.nfl.com/media/3672/big-data-bowl-vonder-haar.pdf, route-ID https://arxiv.org/abs/1908.02423 |
| **License / access terms** | Per-competition Kaggle rules; account + rules acceptance required. Typically usable for the competition and non-commercial research; **redistribution generally not permitted**. Verify the specific competition's rules before any use beyond offline research. |
| **Runtime category** | **Offline training / benchmark only.** Normalized locally into JSONL artifacts; never in the same-session or nightly production path. |
| **Secret / key requirement** | `KAGGLE_USERNAME` + `KAGGLE_API_TOKEN` (**not** `KAGGLE_KEY`). Used only at manual download time; bridged to the `kaggle` CLI's `KAGGLE_KEY` var locally. Never exposed to frontend, browser bundles, logs, PR/issue text, stored artifacts, coach-visible errors, or the database. Not read by the backend, so **not** in `backend/app/config.py`. **No value committed.** |
| **Data privacy risk** | Public NFL competition data; no Toledo PII, medical, or recruiting data. BDB labels must not be presented as Toledo labels. |
| **Model-router / registry path** | N/A — data normalizer, **no model code introduced**, no router/registry path. Any future model trained on these artifacts must route via `select_model(stage, priority)` and default nightly-only until benchmarked. |
| **Overlap with closed decisions** | None. Single-camera (#101), pgvector (#8/#77), SAM (#74) untouched. CFBD (#160–#163) remains authoritative for college data. |
| **Calibrated-tracking dependency** | BDB coordinates are clean ground-truth field yards; Football-IQ derives field coordinates via #127/#128/#129. BDB is offline-only **until** Toledo validation proves transfer — recorded in every artifact manifest. |
| **Football-IQ usage** | Issue #164 — `gpu-worker/datasets/bdb/` offline adapter + benchmark, run via `python -m datasets.bdb`. Feeds offline #139/#140/#141/#150. Raw + normalized data are gitignored; only a synthetic sample is committed. |
| **Notes** | No Kaggle data committed. No token logged/printed/committed. Schema report: [`reports/spike-issue164-bdb-adapter.md`](reports/spike-issue164-bdb-adapter.md). |

---

## Field visualization evaluation — sportypy / sportyR / cfbplotR (Issue #169)

| Field | Detail |
|---|---|
| **Resources evaluated** | `sportypy` (Python, MIT, https://sportypy.sportsdataverse.org); `sportyR` (R, MIT, https://github.com/sportsdataverse/sportyR); `cfbplotR` (R, MIT, https://github.com/sportsdataverse/cfbplotR) |
| **Sport coverage** | American / college football ✅ |
| **Decision** | **Not adopted as runtime dependencies.** Interactive overlays use frontend-native SVG (`frontend/src/components/field-diagram.tsx`). `sportypy` is **deferred** for possible future *offline* Python report plots; R packages (`sportyR`, `cfbplotR`) are **rejected** as a production dependency (no R runtime). See [`docs/adr/0002-field-visualization.md`](docs/adr/0002-field-visualization.md). |
| **Secret / key requirement** | None for any of them. |
| **License impact** | All MIT; none are currently installed/redistributed. A `LICENSES.md` row + dependency add is required *if* `sportypy` is later adopted. |

---

## BoT-SORT (tracker)

| Field | Detail |
|---|---|
| **Component** | BoT-SORT multi-object tracker (ECC camera-motion compensation + ReID) |
| **Source** | https://github.com/NirAharon/BoT-SORT · paper https://arxiv.org/abs/2206.14651 |
| **Sport coverage** | Sport-agnostic tracking algorithm; applied to American-football player tracking only |
| **License** | MIT (reference implementation) |
| **Access / key** | None — Football-IQ ships its **own pure-NumPy adapter** (`gpu-worker/pipeline/tracking/botsort_adapter.py`), not the upstream package; no install, no key |
| **Weights** | None committed. Appearance ReID is optional and rides on detection embeddings; camera-motion warps come from `pipeline.homography.camera_motion_ecc` (Issue #138) |
| **Runtime category** | Nightly-only tracker variant (`botsort`); ~2 GB when paired with a ReID model |
| **Router path** | `pipeline.model_router` → nightly `track` via `ENABLE_BOTSORT_NIGHTLY`; on `NIGHTLY_ONLY_VARIANTS` so same-session is hard-blocked |
| **Privacy risk** | None beyond existing player-tracking data; single-camera only (Issue #101) |
| **Football-IQ usage** | Phase CV — Issue #129. Selectable through the router; never bypasses it. |

---

## StrongSORT (tracker)

| Field | Detail |
|---|---|
| **Component** | StrongSORT multi-object tracker (matching cascade + appearance EMA), offline IDF1-optimised |
| **Source** | https://github.com/dyhBUPT/StrongSORT · paper https://arxiv.org/abs/2202.13514 |
| **Sport coverage** | Sport-agnostic tracking algorithm; applied to American-football player tracking only |
| **License** | GPL-3.0 (reference implementation) — **not vendored**; Football-IQ ships an independent pure-NumPy adapter so the GPL code is neither imported nor redistributed |
| **Access / key** | None — `gpu-worker/pipeline/tracking/strongsort_adapter.py`; no install, no key |
| **Weights** | None committed; appearance embeddings optional (detection-supplied) |
| **Runtime category** | Nightly-only tracker variant (`strongsort`); ~3 GB with a ReID model |
| **Router path** | `pipeline.model_router` → nightly `track` via `MODEL_ROUTING_CONFIG`; on `NIGHTLY_ONLY_VARIANTS` so same-session is hard-blocked |
| **Privacy risk** | None beyond existing player-tracking data; single-camera only |
| **Football-IQ usage** | Phase CV — Issue #129. Selectable through the router; never bypasses it. |

---

## PARSeq (jersey-number OCR)

| Field | Detail |
|---|---|
| **Component** | PARSeq scene-text recognition for jersey-number OCR |
| **Source** | https://github.com/baudm/parseq · paper https://arxiv.org/abs/2207.06966 |
| **Sport coverage** | General scene-text OCR; applied to American-football jersey numbers only |
| **License** | Apache-2.0 (code). Model checkpoints are user-provided/trained; none committed |
| **Access / key** | None at build time. Runtime checkpoint via `REID_OCR_MODEL=parseq:/path/to/parseq.pt`; loaded lazily with a Tesseract fallback when absent |
| **Weights** | **Never committed** (`.pt`/`.pth` are git-ignored). Mounted/downloaded at runtime |
| **Runtime category** | Nightly-only `reid` variant (`parseq-ocr`); torch-backed |
| **Router path** | `pipeline.model_router` → nightly `reid`; on `NIGHTLY_ONLY_VARIANTS`. Same-session `reid` stays Tesseract `jersey-ocr` |
| **Privacy risk** | Reads jersey numbers (no PII/medical); single-camera only |
| **Football-IQ usage** | Phase CV — Issue #131: `gpu-worker/pipeline/tracking/parseq_ocr_adapter.py`, wired into `stage_reid` ahead of Tesseract on the nightly path. |

---

## SportQA — evaluated, not adopted (Issue #168)

| Field | Detail |
|---|---|
| **Resource** | SportQA — sports-understanding text QA benchmark (70,592 multiple-choice questions, 35 sports, 3 difficulty levels) |
| **Source** | https://github.com/haotianxia/SportQA · paper https://arxiv.org/abs/2402.15862 (NAACL 2024) |
| **Sport coverage** | 35 sports; **American football ✅ present** (also contains soccer — that subset is excluded per the denylist) |
| **License** | **CC-BY-4.0** (attribution). Redistribution permitted under CC-BY; raw data kept local/gitignored regardless |
| **Access / key** | Public download; **no API key**. Not read by the backend |
| **Runtime category** | **Offline evaluation only** — never same-session/nightly, not coach-facing, not in the model router |
| **Privacy risk** | None — public sports-knowledge QA; benchmark scores are **not** Toledo labels |
| **Football-IQ usage** | **Deferred** (Issue #168). Coverage harness only: `gpu-worker/eval/sportqa_sportr_eval.py`. Adopt for offline assistant-rules eval only when a coach-facing assistant is scoped. See `reports/spike-issue168-sportqa-sportr.md`. |

---

## SportR — evaluated, deferred to full release (Issue #168)

| Field | Detail |
|---|---|
| **Resource** | SportR — multimodal sports-reasoning benchmark (4,789 images, 2,052 videos, 20k+ QA, 6,841 chain-of-thought, bbox grounding) |
| **Source** | https://github.com/chili-lab/SportR · preprint https://arxiv.org/abs/2511.06499 (ICLR 2026) |
| **Sport coverage** | 5 ball/racket sports — basketball, soccer, table tennis, badminton, **American football ✅**. Soccer subset excluded per the denylist |
| **License** | **Apache-2.0**. **Full dataset staged for release "before ICLR 2026" — verify availability before any use** |
| **Access / key** | Public release; **no key known**. Not read by the backend |
| **Runtime category** | **Offline evaluation only** — not coach-facing, not in the pipeline; single-camera product (#101) means its broadcast frames are an external probe, never a capture mode |
| **Privacy risk** | Public broadcast imagery (verify the released split); no Toledo data; its annotations are **not** calibrated Football-IQ coordinates (#127–#129) |
| **Football-IQ usage** | **Deferred** until full release + license re-check (Issue #168). Same offline harness as SportQA. See `reports/spike-issue168-sportqa-sportr.md`. |

---

## OpenAI CLIP ViT-B/32 (play embeddings + text-tower search)

| Field | Detail |
|---|---|
| **Model** | OpenAI CLIP ViT-B/32 (frozen) — image tower + text tower |
| **Source URL** | https://github.com/openai/CLIP · model card https://github.com/openai/CLIP/blob/main/model-card.md · open-weights via https://github.com/mlfoundations/open_clip (`ViT-B-32`, `openai`) |
| **Sport coverage** | Sport-agnostic vision-language model; applied to **American football** clips and an American-football-only concept vocabulary. Not soccer. |
| **Toledo / MAC relevance** | Broad American football — encodes Toledo clip keyframes (image tower) and football concept phrases (text tower). |
| **License** | MIT (CLIP reference code). Pretrained weights released for research use; re-verify before any external/commercial deployment. |
| **Access / key** | `pip install open_clip_torch` (or `transformers`); **no API key**. Not gated, no token required. |
| **Secret / key requirement** | None. |
| **Runtime category** | Nightly-only embedding encoder (image tower, `stage_embed` — Issues #8/#77) + **offline** text-tower encode for the committed concept catalog (Issue #195). Never same-session. |
| **Data privacy risk** | None beyond existing player-tracking imagery; single-camera only (#101). The committed catalog holds CLIP **text** vectors of generic football phrases — no PII, no Toledo footage. |
| **Model-router / registry path** | `pipeline.model_router` → nightly `embeddings` = `play-embed-clip-vitb32-baseline`; on `NIGHTLY_ONLY_VARIANTS`. Issue #195 adds **no** new stage/variant — it persists the raw 512-d image embedding to `playembeddings.clip_vector` and serves `/api/v1/search/text` from it. |
| **Calibrated-tracking dependency** | None (#127/#128/#129 not implicated). |
| **Weights** | **Never committed** (`.pt`/`.pth`/`.safetensors` git-ignored). Downloaded at runtime by `open_clip`/`transformers`. The committed `backend/app/data/concept_catalog.json` holds **vectors only** (CLIP text-tower outputs), produced offline by `gpu-worker/scripts/build_concept_catalog.py` — never weights. |
| **Football-IQ usage** | Phase 3 — Issues #8/#77 (fused play embedding) and #195 (raw `clip_vector(512)` + CLIP text-tower `/search/text`). Results are always experimental/approximate and never promote to a label. |

---

## Frontend UI libraries (PR #262)

| Field | Detail |
|---|---|
| **Libraries** | `class-variance-authority`, `clsx`, `radix-ui`, `@radix-ui/react-slot`, `sonner`, `tailwind-merge` |
| **License** | MIT (each library) |
| **Access** | `npm install` via `frontend/package.json`; no account or API key required |
| **Football-IQ usage** | Frontend UI primitives and utilities in `frontend/src/components/ui/` and `frontend/src/lib/utils.ts` |
| **Secret / key requirement** | None |
| **Sport coverage** | N/A (UI/tooling libraries only) |

---

## Roboflow SDK (dataset/training tooling)

| | |
|---|---|
| **Libraries** | `roboflow` (Python SDK), `PyYAML` |
| **License** | Apache-2.0 (`roboflow`), MIT (`PyYAML`) |
| **Access** | `pip install -r gpu-worker/requirements-roboflow.txt`; the SDK talks to the team's own private Roboflow workspace |
| **Football-IQ usage** | Owner-side dataset/training tooling only (`gpu-worker/roboflow_ops/`, `training/detect/`): consolidation, frame sampling, auto-label kickoff, active-learning export, dataset download, hosted-train kickoff. Never in the production request path; lazy-imported so CI stub mode runs without it. |
| **Secret / key requirement** | `ROBOFLOW_API_KEY` (gpu-worker `.env` only; never frontend/backend/logs) |
| **Sport coverage** | American football only — operates on the team's own footage and the workspace's own annotations; imports no external datasets |

---

## Dependency Gating Policy

- Any new model dependency must be added to this file **before** the implementing PR is merged.
- New external resources (datasets, models, APIs, libraries) must clear the rubric, soccer/association-football denylist, and license gate in [`docs/external-resource-rubric.md`](docs/external-resource-rubric.md). Football-IQ is an American football platform — soccer resources are rejected.
- CI includes a license-allowlist check (see `.github/workflows/ci.yml`) that fails if a new package is missing from `LICENSES.md`.
- Model weights (`.pt`, `.pth`, `.onnx`, `.engine`) must never be committed to the repository. Download at runtime using `HF_TOKEN` (Hugging Face) or `NGC_API_KEY` (NVIDIA NGC).
- For any model with a gated or non-commercial license, Football-IQ's use case (university-internal, non-commercial coaching analytics) must be re-verified before any external or commercial deployment.

---

*Last updated: May 26, 2026*
# Apache ECharts

Apache ECharts (frontend statistical chart adapter), Apache-2.0. Official project: https://echarts.apache.org/. No model weights or datasets included. Used for cohort charts with accessible HTML tables and linked film selection.
