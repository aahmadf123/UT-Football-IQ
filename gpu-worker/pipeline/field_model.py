"""Yard-number detector for automatic field registration.

Resolution order: the registry's production ``field`` model, else the file
at ``MODEL_FIELD_PATH``. When neither exists the pipeline keeps the classical
line-only calibration (no error): field registration simply has no numeral
anchors. Ultralytics imports lazily, per this repo's CI contract.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import structlog

from pipeline.runtime import inference_device

log = structlog.get_logger(__name__)

#: Detections from the yard-number detector: ``(class_name, confidence, box)``.
Detection = tuple[str, float, Sequence[float]]

_lock = threading.Lock()
_cached: tuple[str, Any] | None = None


def preprocess(frame: Any, size: int = 640) -> Any:
    """Match the training export: stretch to a square, grayscale, and
    contrast-stretch (Roboflow "Auto-contrast via contrast stretching")."""
    import cv2
    import numpy as np

    square = cv2.resize(frame, (size, size), interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(square, cv2.COLOR_BGR2GRAY)
    low, high = np.percentile(gray, (2, 98))
    if high > low:
        gray = np.clip((gray.astype(np.float32) - low) * (255.0 / (high - low)), 0, 255).astype(
            np.uint8
        )
    return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)


class FieldNumberDetector:
    """Ultralytics YOLO wrapper returning named yard-number boxes."""

    def __init__(self, model_path: str, *, confidence: float = 0.3, imgsz: int = 640) -> None:
        from ultralytics import YOLO  # type: ignore[import-untyped]

        self._model = YOLO(model_path)
        self._device = inference_device()
        self._confidence = confidence
        self._imgsz = imgsz
        self.path = model_path

    def __call__(self, frame: Any) -> list[Detection]:

        started = time.perf_counter()
        # The training export stretched every frame to a square, so the model
        # knows numerals at that aspect: stretch the same way (not letterbox)
        # and scale boxes back to the frame.
        h, w = frame.shape[:2]
        result = self._model.predict(
            preprocess(frame, self._imgsz),
            imgsz=self._imgsz,
            conf=self._confidence,
            device=self._device,
            verbose=False,
        )[0]
        sx, sy = w / self._imgsz, h / self._imgsz
        names = result.names
        out: list[Detection] = []
        for (x1, y1, x2, y2), cls, conf in zip(
            result.boxes.xyxy.tolist(), result.boxes.cls.tolist(), result.boxes.conf.tolist()
        ):
            out.append((str(names[int(cls)]), float(conf), [x1 * sx, y1 * sy, x2 * sx, y2 * sy]))
        log.debug(
            "field_numbers_detected",
            count=len(out),
            ms=round((time.perf_counter() - started) * 1000, 1),
        )
        return out


def _model_path() -> str | None:
    try:
        from pipeline import model_registry_client

        resolved = model_registry_client.resolve("field")
        if resolved is not None:
            return str(resolved.local_path)
    except Exception as exc:  # noqa: BLE001 - the registry is optional here
        log.warning("field_model_registry_unavailable", error_type=type(exc).__name__)
    configured = os.environ.get("MODEL_FIELD_PATH", "").strip()
    if configured and Path(configured).is_file():
        return configured
    return None


def get_field_detector() -> FieldNumberDetector | None:
    """The process-wide detector, or ``None`` when no field model is installed."""
    global _cached
    path = _model_path()
    if path is None:
        return None
    with _lock:
        if _cached is not None and _cached[0] == path:
            return _cached[1]  # type: ignore[no-any-return]
        try:
            detector = FieldNumberDetector(path)
        except Exception as exc:  # noqa: BLE001 - fall back to line-only calibration
            log.warning("field_model_load_failed", path=path, error_type=type(exc).__name__)
            return None
        _cached = (path, detector)
        log.info("field_model_loaded", path=path)
        return detector
