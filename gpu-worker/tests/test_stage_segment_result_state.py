"""Same-session clip result-state wiring in the worker (Issue #147).

The segment stage tags each clip it creates with a result tier so the backend /
UI can show a "Preliminary" badge on same-session first-pass clips and clear it
once nightly upgrades them:

  - same-session priority (>= 10) → ``preliminary``
  - nightly priority      (< 10)  → ``final``

Also covers the ``backend`` client helpers: ``create_clip`` forwarding
``result_state`` and the ``finalize_video_clips`` nightly-upgrade call.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from queue.same_session_queue import NIGHTLY_PRIORITY, SAME_SESSION_PRIORITY

from pipeline import backend, stage_segment


class _FakeSegmenter:
    source = "stub"

    def segment(self, _video_path: Path) -> list[dict[str, Any]]:
        return []  # no interior boundaries → one full-length clip


# ── stage_segment: priority → result_state ────────────────────────────────────


@pytest.mark.parametrize(
    ("priority", "expected"),
    [
        (SAME_SESSION_PRIORITY, "preliminary"),
        (SAME_SESSION_PRIORITY + 5, "preliminary"),
        (NIGHTLY_PRIORITY, "final"),
        (SAME_SESSION_PRIORITY - 1, "final"),
    ],
)
def test_segment_maps_priority_to_result_state(
    priority: int, expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    def _fake_write_clips(
        video_id: str,
        boundaries: list[dict[str, Any]],
        duration: float,
        job_id: str,
        boundary_model: str,
        result_state: str | None = None,
    ) -> list[dict[str, Any]]:
        captured["result_state"] = result_state
        return []

    monkeypatch.setattr(stage_segment, "_video_duration", lambda _p: 10.0)
    monkeypatch.setattr(stage_segment, "_write_clips", _fake_write_clips)

    stage_segment._segment("vid-1", Path("/tmp/x.mp4"), "job-1", _FakeSegmenter(), priority)

    assert captured["result_state"] == expected


def test_write_clips_forwards_result_state_to_create_clip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def _fake_create_clip(video_id: str, **kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"id": f"clip-{len(calls)}"}

    monkeypatch.setattr(backend, "create_clip", _fake_create_clip)

    # Empty interior boundaries + 10s duration → one 0..10 clip (> MIN_PLAY_DURATION).
    clips = stage_segment._write_clips("vid-1", [], 10.0, "job-1", "stub", "preliminary")

    assert len(clips) == 1
    assert calls[0]["result_state"] == "preliminary"


# ── backend client: create_clip + finalize_video_clips ────────────────────────


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeClient:
    """Minimal httpx.Client stand-in capturing the last request."""

    def __init__(self, payload: dict[str, Any], sink: dict[str, Any]) -> None:
        self._payload = payload
        self._sink = sink

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def post(self, url: str, json: dict[str, Any] | None = None) -> _FakeResponse:
        self._sink["url"] = url
        self._sink["json"] = json
        return _FakeResponse(self._payload)


def test_create_clip_includes_result_state_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    sink: dict[str, Any] = {}
    # BACKEND_API_URL must be non-empty or create_clip short-circuits into
    # offline mode and never reaches the fake client.
    monkeypatch.setattr(backend, "BACKEND_API_URL", "http://backend.test")
    monkeypatch.setattr(backend, "_client", lambda: _FakeClient({"id": "c1"}, sink))

    backend.create_clip("vid-1", 0.0, 5.0, result_state="preliminary")

    assert sink["json"]["result_state"] == "preliminary"


def test_create_clip_omits_result_state_when_none(monkeypatch: pytest.MonkeyPatch) -> None:
    sink: dict[str, Any] = {}
    monkeypatch.setattr(backend, "BACKEND_API_URL", "http://backend.test")
    monkeypatch.setattr(backend, "_client", lambda: _FakeClient({"id": "c1"}, sink))

    backend.create_clip("vid-1", 0.0, 5.0)

    assert "result_state" not in sink["json"]


def test_finalize_video_clips_posts_and_returns_count(monkeypatch: pytest.MonkeyPatch) -> None:
    sink: dict[str, Any] = {}
    monkeypatch.setattr(backend, "BACKEND_API_URL", "http://backend.test")
    monkeypatch.setattr(
        backend, "_client", lambda: _FakeClient({"finalized_count": 7}, sink)
    )

    count = backend.finalize_video_clips("vid-1")

    assert count == 7
    assert sink["url"] == "/api/v1/videos/vid-1/clips/finalize"


def test_finalize_video_clips_returns_zero_when_backend_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backend, "BACKEND_API_URL", "")
    assert backend.finalize_video_clips("vid-1") == 0
