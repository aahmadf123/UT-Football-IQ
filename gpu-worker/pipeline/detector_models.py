"""Detector adapters for the Player Detection stage (Issue #74).

Mirrors the ``pose_estimator`` adapter pattern: one abstract base, one
production wrapper per model family, and a deterministic stub for tests.

Detection schema (returned by ``detect`` on every adapter):

    {
        "bbox": [x1, y1, x2, y2],         # float pixel coords (required)
        "confidence": 0.0..1.0,             # float (required)
        "class": "player" | "ball",         # str (required)
        "mask": {                           # optional, added by mask-aware
            "format": "polygon",            # detectors (e.g. SAM3Detector).
            "points": [[x, y], ...]         # Downstream consumers MUST treat
        },                                  # this field as optional.
    }

The ``mask`` key is the only schema extension introduced for SAM 3.1.
Everything that runs on YOLO continues to produce dicts without ``mask``
and downstream stages keep working unchanged.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from typing import Any

import numpy as np
import structlog

log = structlog.get_logger(__name__)

# Detection schema constants — used by SAM3Detector and any future
# mask-producing detector to label the mask representation.
MASK_FORMAT_POLYGON = "polygon"

# Default class id → name mapping (COCO).  Both YOLO and SAM 3 in
# Ultralytics use COCO ids, so the mapping is shared.
COCO_CLASS_NAMES: dict[int, str] = {0: "player", 32: "ball"}


# ── Base class ────────────────────────────────────────────────────────────────


class DetectorBase(ABC):
    """Abstract base for per-frame detectors.

    Subclasses must implement ``detect(frame) → list[dict]``.  Each dict
    follows the schema documented at the top of this module.  Returning an
    empty list is valid — it means no objects of interest were found.
    """

    #: Whether this detector populates the optional ``mask`` field.
    produces_masks: bool = False

    @abstractmethod
    def detect(self, frame: np.ndarray) -> list[dict[str, Any]]:
        """Detect objects in a single frame.

        Args:
            frame: BGR uint8 numpy array (H × W × 3).

        Returns:
            List of detection dicts.  See module docstring for schema.
        """


# ── YOLO adapter (existing same-session detector) ─────────────────────────────


class YOLODetector(DetectorBase):
    """Ultralytics YOLO detector — the production same-session path.

    Conditionally imports ultralytics at construction time.  When the
    package or weights are unavailable, callers should fall back to
    ``StubDetector`` rather than crashing.
    """

    produces_masks = False

    def __init__(
        self,
        model_path: str = "yolov8n.pt",
        confidence_threshold: float = 0.35,
        class_map: dict[int, str] | None = None,
    ) -> None:
        try:
            from ultralytics import YOLO  # type: ignore[import-untyped]
        except ImportError as exc:
            raise ImportError(
                "ultralytics is required for YOLODetector. "
                "Install with: pip install ultralytics"
            ) from exc

        self._model = YOLO(model_path)
        self._conf = confidence_threshold
        self._class_map = class_map or COCO_CLASS_NAMES
        log.info("yolo_detector_initialized", model_path=model_path,
                 conf=confidence_threshold)

    def detect(self, frame: np.ndarray) -> list[dict[str, Any]]:
        results = self._model(frame, conf=self._conf, verbose=False)
        detections: list[dict[str, Any]] = []
        for r in results:
            for box in r.boxes:
                cls_id = int(box.cls[0])
                if cls_id not in self._class_map:
                    continue
                x1, y1, x2, y2 = box.xyxy[0].tolist()
                detections.append({
                    "bbox": [float(x1), float(y1), float(x2), float(y2)],
                    "confidence": float(box.conf[0]),
                    "class": self._class_map[cls_id],
                })
        return detections


# ── SAM 3.1 adapter (experimental nightly detector) ───────────────────────────


class SAM3Detector(DetectorBase):
    """Meta SAM 3.1 detector via Ultralytics — experimental nightly path.

    SAM 3.1 is a promptable segmentation model.  Loaded through
    Ultralytics (>= 8.3.237) it exposes a YOLO-like inference API that
    returns both boxes *and* per-instance masks.  We surface masks as
    polygon point lists in the optional ``mask`` field of each detection
    so the bbox schema stays backward compatible.

    Auth: weights live in ``facebook/sam3`` on Hugging Face and are gated.
    The Ultralytics loader picks up ``HF_TOKEN`` from the process env to
    download them; no token is committed to the repo.
    """

    produces_masks = True

    def __init__(
        self,
        model_path: str = "sam3.1.pt",
        confidence_threshold: float = 0.35,
        class_map: dict[int, str] | None = None,
        text_prompts: list[str] | None = None,
    ) -> None:
        try:
            from ultralytics import SAM  # type: ignore[import-untyped]
        except ImportError as exc:
            raise ImportError(
                "ultralytics>=8.3.237 is required for SAM3Detector. "
                "Install with: pip install -U ultralytics"
            ) from exc

        if not os.environ.get("HF_TOKEN"):
            log.warning(
                "sam3_detector_no_hf_token",
                advice="Weight download may fail. Set HF_TOKEN env var.",
            )

        self._model = SAM(model_path)
        self._conf = confidence_threshold
        self._class_map = class_map or COCO_CLASS_NAMES
        # SAM 3 supports open-vocabulary text prompts.  Default to football
        # roles we already track downstream — passes through as the
        # ``class`` field instead of relying on COCO ids.
        self._text_prompts = text_prompts or ["person", "ball"]
        log.info("sam3_detector_initialized", model_path=model_path,
                 conf=confidence_threshold, prompts=self._text_prompts)

    def detect(self, frame: np.ndarray) -> list[dict[str, Any]]:
        results = self._model(
            frame,
            conf=self._conf,
            verbose=False,
            prompts=self._text_prompts,
        )
        detections: list[dict[str, Any]] = []
        for r in results:
            boxes = getattr(r, "boxes", None)
            masks = getattr(r, "masks", None)
            if boxes is None:
                continue
            for i, box in enumerate(boxes):
                cls_id = int(box.cls[0])
                cls_name = self._class_map.get(cls_id, "player")
                x1, y1, x2, y2 = box.xyxy[0].tolist()
                det: dict[str, Any] = {
                    "bbox": [float(x1), float(y1), float(x2), float(y2)],
                    "confidence": float(box.conf[0]),
                    "class": cls_name,
                }
                if masks is not None and i < len(masks.xy):
                    polygon = masks.xy[i]
                    det["mask"] = {
                        "format": MASK_FORMAT_POLYGON,
                        "points": [[float(p[0]), float(p[1])] for p in polygon],
                    }
                detections.append(det)
        return detections


# ── Stub detector (for tests and CI without GPU / weights) ────────────────────


class StubDetector(DetectorBase):
    """Deterministic synthetic detector for tests.

    Emits a small fixed grid of player bounding boxes, optionally with
    synthetic polygon masks.  No model weights, no GPU, no network
    required — safe to use anywhere ``ultralytics`` would be too heavy.
    """

    def __init__(
        self,
        n_players: int = 4,
        produces_masks: bool = False,
        seed: int = 42,
    ) -> None:
        self.produces_masks = produces_masks
        self._n_players = n_players
        rng = np.random.default_rng(seed)
        self._jitter = rng.uniform(-2.0, 2.0, (n_players, 4))
        # Stride players evenly across a notional 1280×720 frame so tracker
        # tests have non-overlapping starting boxes.
        self._templates: list[dict[str, Any]] = []
        for i in range(n_players):
            cx = 100.0 + i * (1080.0 / max(n_players - 1, 1))
            cy = 360.0
            x1, y1, x2, y2 = cx - 30.0, cy - 60.0, cx + 30.0, cy + 60.0
            x1 += float(self._jitter[i][0])
            y1 += float(self._jitter[i][1])
            x2 += float(self._jitter[i][2])
            y2 += float(self._jitter[i][3])
            det: dict[str, Any] = {
                "bbox": [x1, y1, x2, y2],
                "confidence": 0.85 - 0.01 * i,
                "class": "player",
            }
            if produces_masks:
                det["mask"] = {
                    "format": MASK_FORMAT_POLYGON,
                    "points": [
                        [x1, y1], [x2, y1], [x2, y2], [x1, y2],
                    ],
                }
            self._templates.append(det)

    def detect(self, frame: np.ndarray) -> list[dict[str, Any]]:
        # Return fresh dict copies so callers can mutate without
        # corrupting the cached templates between frames.
        out: list[dict[str, Any]] = []
        for t in self._templates:
            det = dict(t)
            det["bbox"] = list(t["bbox"])
            if "mask" in t:
                det["mask"] = {
                    "format": t["mask"]["format"],
                    "points": [list(p) for p in t["mask"]["points"]],
                }
            out.append(det)
        return out


# ── Factory ───────────────────────────────────────────────────────────────────

# Variant identifiers used by ``model_router``.  Keeping them as module-level
# constants means callers do not have to string-match by hand.
YOLOV8N = "yolov8n"
YOLOV8M = "yolov8m"
YOLOV8S = "yolov8s"
SAM3_1 = "sam3.1"
SAM3 = "sam3"
STUB_DETECTOR = "stub"


def get_detector(
    variant: str,
    *,
    model_path: str | None = None,
    confidence_threshold: float = 0.35,
) -> DetectorBase:
    """Return a detector adapter for the given routing variant.

    Logic:
      - ``yolov8*``  → YOLODetector with the matching weight file
      - ``sam3*``    → SAM3Detector (requires HF_TOKEN at runtime)
      - ``stub`` or unknown → StubDetector (deterministic, no deps)

    On ``ImportError`` (e.g. running tests without ultralytics installed)
    the factory falls back to ``StubDetector`` with a warning — same
    self-configuring behaviour as ``pose_estimator.get_estimator``.
    """
    v = (variant or "").lower()

    if v.startswith("sam3"):
        try:
            return SAM3Detector(
                model_path=model_path or f"{v}.pt",
                confidence_threshold=confidence_threshold,
            )
        except ImportError:
            log.warning("sam3_unavailable_using_stub", variant=variant)
            return StubDetector(produces_masks=True)

    if v.startswith("yolo"):
        try:
            return YOLODetector(
                model_path=model_path or f"{v}.pt",
                confidence_threshold=confidence_threshold,
            )
        except ImportError:
            log.warning("yolo_unavailable_using_stub", variant=variant)
            return StubDetector(produces_masks=False)

    if v == STUB_DETECTOR or not v:
        return StubDetector()

    log.warning("unknown_detector_variant_using_stub", variant=variant)
    return StubDetector()


# ── Regime-aware player composition (Issue #128) ──────────────────────────────

# Regimes (kept in sync with pipeline.homography.regime_detector).
_DRONE_FOLLOW = "drone_follow"
_UNCONSTRAINED = "unconstrained"
#: Regimes whose players may be small in frame (high/far/unknown viewpoints):
#: they get the SAHI + dual-resolution composition. ``fixed_sideline`` keeps
#: the plain full-frame pass (press-box footage has large players).
_SMALL_PLAYER_REGIMES = frozenset({_DRONE_FOLLOW, _UNCONSTRAINED})


def player_detection_strategy(regime: str | None) -> str:
    """Strategy label for the player routing artifact, by capture regime.

    ``drone_follow`` players are 30–80 px tall → SAHI tiles + dual-resolution.
    ``unconstrained`` (any angle/height) may put players anywhere on that
    scale → the same recall-first composition. ``fixed_sideline`` players are
    80–200 px → the base detector is enough (Issue #128).
    """
    return "sahi-400+dual-res" if regime in _SMALL_PLAYER_REGIMES else "base"


def build_player_detector(
    variant: str,
    regime: str | None = None,
    *,
    base: DetectorBase | None = None,
    confidence_threshold: float = 0.35,
) -> DetectorBase:
    """Compose the regime-appropriate player detector around a base model.

    The base model is the router-resolved variant (``yolov8n`` same-session,
    ``yolov8m`` nightly, or ``sam3.1`` when ``ENABLE_SAM3_NIGHTLY``). For
    ``drone_follow`` it is wrapped in dual-resolution + SAHI (400 px tiles,
    0.2 overlap) to recover small players; otherwise the base model is
    returned unchanged. Wrapping a router-resolved adapter (never a raw YOLO
    handle) keeps same-session safety intact.
    """
    detector = (
        base
        if base is not None
        else get_detector(variant, confidence_threshold=confidence_threshold)
    )
    if regime not in _SMALL_PLAYER_REGIMES:
        return detector
    # Local imports avoid a package import cycle (detection imports this module).
    from pipeline.detection.dual_res_merger import DualResolutionDetector
    from pipeline.detection.sahi_wrapper import (
        DEFAULT_OVERLAP,
        PLAYER_TILE,
        SAHIDetectorAdapter,
    )

    sahi = SAHIDetectorAdapter(
        detector, tile=PLAYER_TILE, overlap=DEFAULT_OVERLAP, full_frame_pass=True
    )
    return DualResolutionDetector(sahi)


# ── Helpers ───────────────────────────────────────────────────────────────────


def detection_has_mask(detection: dict[str, Any]) -> bool:
    """Return True if ``detection`` carries the optional ``mask`` field."""
    mask = detection.get("mask")
    if not isinstance(mask, dict):
        return False
    points = mask.get("points")
    return isinstance(points, list) and len(points) >= 3


def polygon_area(points: list[list[float]]) -> float:
    """Compute the unsigned area of a polygon by the shoelace formula.

    Returns 0.0 for degenerate polygons (< 3 distinct vertices).
    """
    if len(points) < 3:
        return 0.0
    n = len(points)
    s = 0.0
    for i in range(n):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return abs(s) * 0.5
