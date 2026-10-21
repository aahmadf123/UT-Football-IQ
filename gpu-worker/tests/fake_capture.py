"""A stand-in for ``cv2.VideoCapture`` that behaves like a real decoder.

Frames carry presentation timestamps (``CAP_PROP_POS_MSEC`` of the last
grabbed frame), ``grab``/``retrieve``/``read`` advance one frame and stop at
the end, and ``set`` seeks by time or frame. ``missing`` lists frame indices
the decoder "cannot reconstruct": they are skipped, as real decoders do.
"""

from __future__ import annotations

from typing import Any

import cv2
import numpy as np


class FakeCapture:
    def __init__(
        self,
        frames: int = 3,
        *,
        fps: float = 30.0,
        width: int = 100,
        height: int = 100,
        missing: set[int] | None = None,
        image: Any = None,
    ) -> None:
        self.total = frames
        self.fps = fps
        self.width = width
        self.height = height
        self.missing = missing or set()
        self.image = image
        self._next = 0
        self._current: int | None = None
        self.retrieved: list[int] = []

    def isOpened(self) -> bool:  # noqa: N802 - cv2 API
        return True

    def release(self) -> None:
        pass

    def get(self, prop: int) -> float:
        if prop == cv2.CAP_PROP_POS_MSEC:
            return 0.0 if self._current is None else self._current / self.fps * 1000.0
        if prop == cv2.CAP_PROP_FPS:
            return self.fps
        if prop == cv2.CAP_PROP_FRAME_COUNT:
            return float(self.total)
        if prop == cv2.CAP_PROP_FRAME_WIDTH:
            return float(self.width)
        if prop == cv2.CAP_PROP_FRAME_HEIGHT:
            return float(self.height)
        if prop == cv2.CAP_PROP_POS_FRAMES:
            return float(self._next)
        return 0.0

    def set(self, prop: int, value: float) -> bool:
        if prop == cv2.CAP_PROP_POS_MSEC:
            self._next = max(0, int(round(value / 1000.0 * self.fps)))
        elif prop == cv2.CAP_PROP_POS_FRAMES:
            self._next = max(0, int(value))
        self._current = None
        return True

    def grab(self) -> bool:
        while self._next in self.missing:
            self._next += 1
        if self._next >= self.total:
            self._current = None
            return False
        self._current = self._next
        self._next += 1
        return True

    def retrieve(self) -> tuple[bool, Any]:
        if self._current is None:
            return False, None
        self.retrieved.append(self._current)
        if self.image is not None:
            return True, self.image.copy()
        frame = np.zeros((self.height, self.width, 3), np.uint8)
        frame[...] = self._current % 256  # each frame is identifiable
        return True, frame

    def read(self) -> tuple[bool, Any]:
        if not self.grab():
            return False, None
        return self.retrieve()
