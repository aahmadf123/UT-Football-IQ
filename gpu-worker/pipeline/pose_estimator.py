"""Pose estimator adapter for the Pose-Lite Biomechanics Pilot (Issue #6).

Provides a uniform interface over RTMPose (via MMPose) and a deterministic
stub for testing without GPU hardware or model weights.

Model stack (from issue #6):
  Primary   : RTMPose-m via MMPose — 430 FPS on NVIDIA GTX 1660 Ti
  Fallback  : StubPoseEstimator — deterministic synthetic keypoints, same schema

Keypoint schema (17-point COCO layout used by both RTMPose-m and ViTPose):
  0  nose          1  left_eye       2  right_eye
  3  left_ear      4  right_ear      5  left_shoulder
  6  right_shoulder 7 left_elbow    8  right_elbow
  9  left_wrist    10 right_wrist   11  left_hip
  12 right_hip     13 left_knee     14 right_knee
  15 left_ankle    16 right_ankle

Each keypoint is returned as:
  {"name": str, "x": float, "y": float, "confidence": float}

  x, y are in pixel coordinates relative to the input frame.
  confidence is 0.0–1.0 (visibility score from the model).
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from typing import Any

import numpy as np
import structlog

log = structlog.get_logger(__name__)

# ── COCO keypoint names (index → name) ────────────────────────────────────────
COCO_KEYPOINT_NAMES: list[str] = [
    "nose",
    "left_eye",
    "right_eye",
    "left_ear",
    "right_ear",
    "left_shoulder",
    "right_shoulder",
    "left_elbow",
    "right_elbow",
    "left_wrist",
    "right_wrist",
    "left_hip",
    "right_hip",
    "left_knee",
    "right_knee",
    "left_ankle",
    "right_ankle",
]

COCO_KEYPOINT_INDEX: dict[str, int] = {name: i for i, name in enumerate(COCO_KEYPOINT_NAMES)}
NUM_KEYPOINTS = len(COCO_KEYPOINT_NAMES)


# ── Base class ────────────────────────────────────────────────────────────────


class PoseEstimatorBase(ABC):
    """Abstract base for all pose estimators.

    Subclasses must implement ``estimate(frame) → list[dict]``.
    """

    @abstractmethod
    def estimate(self, frame: np.ndarray) -> list[dict[str, Any]]:
        """Estimate pose keypoints for a single frame.

        Args:
            frame: BGR uint8 numpy array (H × W × 3).

        Returns:
            List of 17 dicts, one per COCO keypoint:
            ``[{"name": str, "x": float, "y": float, "confidence": float}, ...]``
        """


# ── RTMPose adapter ───────────────────────────────────────────────────────────


class RTMPoseEstimator(PoseEstimatorBase):
    """RTMPose-m estimator via MMPose.

    Conditionally imports mmpose at construction time; raises ``ImportError`` if
    the package is not installed (caller should fall back to ``StubPoseEstimator``).

    Args:
        model_path: Path to RTMPose .pth weights file.
        config_path: Optional path to mmpose config yaml.  When ``None``, the
                     bundled RTMPose-m config is used.
        device: Torch device string (default "cuda:0").
    """

    def __init__(
        self,
        model_path: str,
        config_path: str | None = None,
        device: str = "cuda:0",
    ) -> None:
        try:
            from mmpose.apis import inference_topdown, init_model  # type: ignore[import]
            from mmpose.utils import register_all_modules  # type: ignore[import]
        except ImportError as exc:
            raise ImportError(
                "mmpose is required for RTMPoseEstimator. "
                "Install with: pip install mmpose"
            ) from exc

        register_all_modules()
        _config = config_path or _default_rtmpose_config()
        self._model = init_model(_config, model_path, device=device)
        self._inference_topdown = inference_topdown
        log.info("rtmpose_estimator_initialized", model_path=model_path, device=device)

    def estimate(self, frame: np.ndarray) -> list[dict[str, Any]]:
        """Run RTMPose inference on one frame and return 17 COCO keypoints."""
        # inference_topdown expects RGB; our pipeline uses BGR
        rgb = frame[:, :, ::-1]
        results = self._inference_topdown(self._model, rgb)
        if not results:
            return _zero_keypoints()
        # Take the highest-confidence person if multiple detected
        best = max(results, key=lambda r: float(r.pred_instances.scores[0]))
        kps = best.pred_instances.keypoints[0]       # shape (17, 2)
        scores = best.pred_instances.keypoint_scores[0]  # shape (17,)
        return [
            {"name": COCO_KEYPOINT_NAMES[i], "x": float(kps[i][0]),
             "y": float(kps[i][1]), "confidence": float(scores[i])}
            for i in range(NUM_KEYPOINTS)
        ]


def _default_rtmpose_config() -> str:
    """Return the bundled RTMPose-m config path (relative to mmpose install)."""
    try:
        from pathlib import Path as _P

        import mmpose  # type: ignore[import]
        cfg = (
            _P(mmpose.__file__).parent
            / "configs"
            / "body_2d_keypoint"
            / "rtmpose"
            / "coco"
            / "rtmpose-m_8xb256-420e_coco-256x192.py"
        )
        return str(cfg)
    except Exception:
        return "rtmpose-m_8xb256-420e_coco-256x192.py"


# ── Stub estimator (for tests and CI) ─────────────────────────────────────────


class StubPoseEstimator(PoseEstimatorBase):
    """Deterministic synthetic pose estimator.

    Returns the same 17 COCO keypoints every call for a given seed, regardless
    of the input frame content.  This allows full downstream biomechanics logic
    to be unit-tested without GPU hardware or video footage.

    The synthetic keypoints approximate a standing human player on a 1280×720
    frame:
      - Head cluster near top-centre
      - Shoulder width ~20% of frame width
      - Hip–knee–ankle chain going downward
      - All confidence scores seeded from the RNG (0.6–0.95 range)

    Args:
        seed: NumPy RNG seed (default 42).  Use different seeds per player to
              simulate multiple distinct pose tracks in the same clip.
    """

    # Approximate anatomical layout for a 1280×720 frame, normalised 0–1
    _SKELETON_NORM: list[tuple[float, float]] = [
        (0.50, 0.08),   # 0  nose
        (0.48, 0.07),   # 1  left_eye
        (0.52, 0.07),   # 2  right_eye
        (0.46, 0.09),   # 3  left_ear
        (0.54, 0.09),   # 4  right_ear
        (0.42, 0.22),   # 5  left_shoulder
        (0.58, 0.22),   # 6  right_shoulder
        (0.39, 0.38),   # 7  left_elbow
        (0.61, 0.38),   # 8  right_elbow
        (0.37, 0.52),   # 9  left_wrist
        (0.63, 0.52),   # 10 right_wrist
        (0.44, 0.55),   # 11 left_hip
        (0.56, 0.55),   # 12 right_hip
        (0.43, 0.74),   # 13 left_knee
        (0.57, 0.74),   # 14 right_knee
        (0.42, 0.93),   # 15 left_ankle
        (0.58, 0.93),   # 16 right_ankle
    ]
    _FRAME_W = 1280
    _FRAME_H = 720

    def __init__(self, seed: int = 42) -> None:
        self._seed = seed
        rng = np.random.default_rng(seed)
        # Pixel-space jitter (±5 px) per keypoint — fixed per seed for determinism
        jitter_x = rng.uniform(-5, 5, NUM_KEYPOINTS)
        jitter_y = rng.uniform(-5, 5, NUM_KEYPOINTS)
        # Confidence in [0.60, 0.95]
        confs = rng.uniform(0.60, 0.95, NUM_KEYPOINTS)

        self._keypoints: list[dict[str, Any]] = [
            {
                "name": COCO_KEYPOINT_NAMES[i],
                "x": self._SKELETON_NORM[i][0] * self._FRAME_W + jitter_x[i],
                "y": self._SKELETON_NORM[i][1] * self._FRAME_H + jitter_y[i],
                "confidence": float(confs[i]),
            }
            for i in range(NUM_KEYPOINTS)
        ]

    def estimate(self, frame: np.ndarray) -> list[dict[str, Any]]:
        # Per-call dict copies: callers that mutate keypoint dicts (e.g. to
        # adjust confidences in tests, or to attach derived fields downstream)
        # must not be able to corrupt the cached skeleton on this estimator.
        return [dict(k) for k in self._keypoints]


# ── NVIDIA BodyPose3DNet adapter (Phase 2.5 spike) ────────────────────────────

# BodyPose3DNet uses a 34-joint skeleton. These indices map to COCO-17.
_BP3D_TO_COCO: dict[int, int] = {
    0: 0,    # nose → nose
    1: 1,    # left_eye → left_eye
    2: 2,    # right_eye → right_eye
    3: 3,    # left_ear → left_ear
    4: 4,    # right_ear → right_ear
    5: 5,    # left_shoulder → left_shoulder
    6: 6,    # right_shoulder → right_shoulder
    7: 7,    # left_elbow → left_elbow
    8: 8,    # right_elbow → right_elbow
    9: 9,    # left_wrist → left_wrist
    10: 10,  # right_wrist → right_wrist
    11: 11,  # left_hip → left_hip
    12: 12,  # right_hip → right_hip
    13: 13,  # left_knee → left_knee
    14: 14,  # right_knee → right_knee
    15: 15,  # left_ankle → left_ankle
    16: 16,  # right_ankle → right_ankle
}


class BodyPose3DNetEstimator(PoseEstimatorBase):
    """NVIDIA BodyPose3DNet adapter (optional, Phase 2.5 spike).

    Loads a BodyPose3DNet ONNX model via ``onnxruntime`` and maps the
    34-joint output to the COCO 17-keypoint layout so all downstream
    biomechanics code works unchanged.

    Gracefully refuses to initialise when:
      - Weights file is missing.
      - VRAM is insufficient (< 700 MB free).
      - Required libraries (``onnxruntime`` / ``onnxruntime-gpu``) are not
        installed.

    Args:
        model_path: Path to ``.onnx`` weights (after the
                    ``bodypose3dnet:`` prefix is stripped by the factory).
        device_id: CUDA device ordinal (default 0).
    """

    def __init__(self, model_path: str, device_id: int = 0) -> None:
        from pathlib import Path as _P

        weights = _P(model_path)
        if not weights.is_file():
            raise FileNotFoundError(
                f"BodyPose3DNet weights not found: {weights}. "
                "Download from NGC: ngc registry model download-version "
                "nvidia/tao/bodypose3dnet:deployable_onnx_v1.0"
            )

        if not _check_vram_mb(700, device_id):
            raise RuntimeError(
                "Insufficient VRAM for BodyPose3DNet (need ~700 MB free). "
                "Falling back to RTMPose or Stub."
            )

        try:
            import onnxruntime as ort  # type: ignore[import-untyped]
        except ImportError as exc:
            raise ImportError(
                "onnxruntime-gpu is required for BodyPose3DNetEstimator. "
                "Install with: pip install onnxruntime-gpu"
            ) from exc

        providers = [("CUDAExecutionProvider", {"device_id": device_id})]
        self._session = ort.InferenceSession(str(weights), providers=providers)
        self._input_name = self._session.get_inputs()[0].name
        input_shape = self._session.get_inputs()[0].shape
        self._input_h = int(input_shape[2]) if len(input_shape) >= 4 else 256
        self._input_w = int(input_shape[3]) if len(input_shape) >= 4 else 192
        log.info(
            "bodypose3dnet_initialized",
            model_path=model_path,
            device_id=device_id,
            input_shape=(self._input_h, self._input_w),
        )

    def estimate(self, frame: np.ndarray) -> list[dict[str, Any]]:
        """Run BodyPose3DNet on one frame and return 17 COCO keypoints."""
        import cv2  # local import to avoid top-level opencv dep in stub path

        h, w = frame.shape[:2]
        resized = cv2.resize(frame, (self._input_w, self._input_h))
        blob = resized.astype(np.float32).transpose(2, 0, 1)[np.newaxis] / 255.0
        outputs = self._session.run(None, {self._input_name: blob})
        if not outputs or outputs[0].size == 0:
            return _zero_keypoints()

        raw_kps = outputs[0][0]  # shape ~ (34, 3) or (N, 34, 3)
        if raw_kps.ndim == 3:
            raw_kps = raw_kps[0]

        result: list[dict[str, Any]] = []
        for coco_idx in range(NUM_KEYPOINTS):
            bp3d_idx = next(
                (k for k, v in _BP3D_TO_COCO.items() if v == coco_idx), None
            )
            if bp3d_idx is not None and bp3d_idx < len(raw_kps):
                kp_data = raw_kps[bp3d_idx]
                result.append({
                    "name": COCO_KEYPOINT_NAMES[coco_idx],
                    "x": float(kp_data[0]) * w / self._input_w,
                    "y": float(kp_data[1]) * h / self._input_h,
                    "confidence": float(kp_data[2]) if len(kp_data) > 2 else 0.8,
                })
            else:
                result.append({
                    "name": COCO_KEYPOINT_NAMES[coco_idx],
                    "x": 0.0, "y": 0.0, "confidence": 0.0,
                })
        return result


def _check_vram_mb(required_mb: int, device_id: int = 0) -> bool:
    """Return True if at least ``required_mb`` of free VRAM is available."""
    import subprocess as _sp

    try:
        out = _sp.check_output(
            [
                "nvidia-smi",
                f"--id={device_id}",
                "--query-gpu=memory.free",
                "--format=csv,noheader,nounits",
            ],
            timeout=5,
            text=True,
        )
        free_mb = int(out.strip())
        return free_mb >= required_mb
    except Exception:
        return False


# ── Factory ───────────────────────────────────────────────────────────────────

_BODYPOSE3D_PREFIX = "bodypose3dnet:"


def get_estimator(model_path: str | None) -> PoseEstimatorBase:
    """Return the appropriate pose estimator.

    Logic:
    - ``bodypose3dnet:/path/to/model.onnx`` → BodyPose3DNetEstimator
    - Any other non-empty path *and* mmpose importable → RTMPoseEstimator
    - Otherwise → StubPoseEstimator with a warning

    This makes the worker self-configuring: set ``MODEL_POSE_PATH`` to activate
    real inference; leave it unset for test / CI / pre-weight environments.
    """
    if model_path and model_path.startswith(_BODYPOSE3D_PREFIX):
        weights = model_path[len(_BODYPOSE3D_PREFIX):]
        try:
            estimator = BodyPose3DNetEstimator(weights)
            log.info("pose_estimator_bodypose3dnet", model_path=weights)
            return estimator
        except (ImportError, FileNotFoundError, RuntimeError) as exc:
            log.warning(
                "bodypose3dnet_unavailable_fallback",
                error=str(exc),
            )

    if model_path and not model_path.startswith(_BODYPOSE3D_PREFIX):
        try:
            estimator = RTMPoseEstimator(model_path)
            log.info("pose_estimator_rtmpose", model_path=model_path)
            return estimator
        except ImportError:
            log.warning(
                "mmpose_not_installed_using_stub",
                model_path=model_path,
                advice="Install mmpose or set MODEL_POSE_PATH='' to silence this warning",
            )
    elif not model_path:
        log.info("pose_estimator_stub_no_model_path")
    return StubPoseEstimator()


# ── Geometry helpers (used by stage_pose.py) ──────────────────────────────────


def kp(keypoints: list[dict[str, Any]], name: str) -> dict[str, Any]:
    """Return the keypoint dict for ``name`` from a keypoints list."""
    for k in keypoints:
        if k["name"] == name:
            return k
    return {"name": name, "x": 0.0, "y": 0.0, "confidence": 0.0}


def angle_degrees(
    a: tuple[float, float],
    b: tuple[float, float],
    c: tuple[float, float],
) -> float:
    """Return the angle (degrees) at vertex *b* in the triangle a–b–c.

    Returns 0.0 if any vector has zero length.
    """
    ba = (a[0] - b[0], a[1] - b[1])
    bc = (c[0] - b[0], c[1] - b[1])
    dot = ba[0] * bc[0] + ba[1] * bc[1]
    mag_ba = math.sqrt(ba[0] ** 2 + ba[1] ** 2)
    mag_bc = math.sqrt(bc[0] ** 2 + bc[1] ** 2)
    if mag_ba < 1e-6 or mag_bc < 1e-6:
        return 0.0
    cos_theta = max(-1.0, min(1.0, dot / (mag_ba * mag_bc)))
    return math.degrees(math.acos(cos_theta))


def vector_angle_from_vertical(
    p1: tuple[float, float],
    p2: tuple[float, float],
) -> float:
    """Return the angle (degrees) between vector p1→p2 and the vertical axis.

    Vertical = (0, -1) in image coords (y increases downward).
    """
    dx = p2[0] - p1[0]
    dy = p2[1] - p1[1]
    length = math.sqrt(dx * dx + dy * dy)
    if length < 1e-6:
        return 0.0
    # Vertical unit vector in image space = (0, -1)
    cos_theta = -dy / length
    return math.degrees(math.acos(max(-1.0, min(1.0, cos_theta))))


def midpoint(
    p1: tuple[float, float],
    p2: tuple[float, float],
) -> tuple[float, float]:
    return ((p1[0] + p2[0]) / 2.0, (p1[1] + p2[1]) / 2.0)


# ── Private helpers ────────────────────────────────────────────────────────────


def _zero_keypoints() -> list[dict[str, Any]]:
    return [{"name": n, "x": 0.0, "y": 0.0, "confidence": 0.0} for n in COCO_KEYPOINT_NAMES]
