"""Jersey-number reading with EasyOCR (GPU when available).

The worker already ships EasyOCR for game-film scoreboards, so it doubles as
the jersey reader: Tesseract and PARSeq are optional extras that are usually
not installed, which left every track unidentified. Numbers are read from
the torso band of the player box (front or back), upscaled so ~20-40 px
drone players are legible, restricted to digits.
"""

from __future__ import annotations

import threading
from typing import Any

import cv2
import numpy as np
import structlog

log = structlog.get_logger(__name__)

#: Torso band of a player box: below the helmet, above the waist.
TORSO_TOP = 0.12
TORSO_BOTTOM = 0.62
MIN_CROP_HEIGHT = 96
MIN_READ_CONFIDENCE = 0.45

_lock = threading.Lock()
_reader: Any | None = None
_unavailable = False


def _get_reader() -> Any | None:
    global _reader, _unavailable
    with _lock:
        if _reader is None and not _unavailable:
            try:
                import easyocr  # type: ignore[import-not-found]

                from pipeline.runtime import inference_device

                gpu = inference_device().startswith("cuda")
                _reader = easyocr.Reader(["en"], gpu=gpu, verbose=False)
            except Exception as exc:  # noqa: BLE001 - optional dependency
                log.info("easyocr_jersey_unavailable", reason=type(exc).__name__)
                _unavailable = True
        return _reader


def torso_crop(frame: np.ndarray, bbox: list[float]) -> np.ndarray | None:
    """The number-bearing torso band of ``bbox``, upscaled for OCR."""
    x1, y1, x2, y2 = [int(round(v)) for v in bbox[:4]]
    h, w = frame.shape[:2]
    height = y2 - y1
    top = max(0, y1 + int(TORSO_TOP * height))
    bottom = min(h, y1 + int(TORSO_BOTTOM * height))
    left, right = max(0, x1), min(w, x2)
    if bottom - top < 6 or right - left < 6:
        return None
    crop = frame[top:bottom, left:right]
    scale = max(1.0, MIN_CROP_HEIGHT / crop.shape[0])
    if scale > 1.0:
        crop = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    return crop


class EasyOCRJerseyReader:
    """``recognize(crop) -> (number | None, confidence)`` like the PARSeq adapter."""

    def __init__(self, reader: Any) -> None:
        self._reader = reader

    def recognize(self, crop: np.ndarray) -> tuple[int | None, float]:
        results = self._reader.readtext(crop, allowlist="0123456789", detail=1)
        best: tuple[int | None, float] = (None, 0.0)
        for _box, text, confidence in results:
            digits = "".join(ch for ch in str(text) if ch.isdigit())
            if 1 <= len(digits) <= 2 and float(confidence) >= MIN_READ_CONFIDENCE:
                if float(confidence) > best[1]:
                    best = (int(digits), float(confidence))
        return best


def get_easyocr_jersey_reader() -> EasyOCRJerseyReader | None:
    reader = _get_reader()
    return EasyOCRJerseyReader(reader) if reader is not None else None
