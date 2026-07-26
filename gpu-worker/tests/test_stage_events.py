"""Integration tests for stage_events (Issues #132 + #134).

Covers the legacy heuristic fallback and the multi-signal path (snap + LOS,
and a full pass with ball state machine + attribution). The backend writer is
monkeypatched so the stage runs without a live API.
"""

from __future__ import annotations

import os
import sys
import uuid
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from pipeline import backend, stage_events

FPS = 30.0
SNAP = 10


@pytest.fixture(autouse=True)
def _fake_backend(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    captured: list[dict[str, Any]] = []

    def fake_create_event(clip_id: str, event_type: str, **kw: Any) -> dict[str, Any]:
        captured.append({"event_type": event_type, **kw})
        return {"id": str(uuid.uuid4())}

    monkeypatch.setattr(backend, "create_event", fake_create_event)
    return captured


def _formation(fx: float = 30.0, n: int = 11) -> list[dict[str, Any]]:
    tracks = []
    for i in range(n):
        pts = []
        for f in range(0, 25):
            x = fx + (0.0 if f < SNAP else 0.4 * (f - SNAP + 1))
            pts.append({"frame_number": f, "field_x": x, "field_y": float(i - n // 2),
                        "bbox": [i * 20.0, 0.0, i * 20.0 + 10, 20.0]})
        tracks.append({"id": f"p{i}", "track_points": pts})
    return tracks


def _ol_pose() -> dict[int, dict[str, dict[str, tuple[float, float]]]]:
    pose: dict[int, dict[str, dict[str, tuple[float, float]]]] = {}
    for f in range(0, 25):
        sx = 100.0 + (40.0 if f >= SNAP else 0.0)
        pose[f] = {
            f"p{i}": {
                "left_shoulder": (sx - 5, 50.0),
                "right_shoulder": (sx + 5, 50.0),
                "left_hip": (95.0, 100.0),
                "right_hip": (105.0, 100.0),
            }
            for i in range(4)
        }
    return pose


# ── Legacy path ─────────────────────────────────────────────────────────────


def test_legacy_heuristic_emits_snap() -> None:
    detections: dict[str, list[dict[str, Any]]] = {}
    # Two players nearly still, then a big jump → heuristic snap.
    for f in range(0, 6):
        shift = 0.0 if f < 3 else 40.0
        detections[str(f)] = [
            {"class": "player", "bbox": [100 + shift, 100, 120 + shift, 140]},
            {"class": "player", "bbox": [200 + shift, 100, 220 + shift, 140]},
        ]
    out = stage_events.run("clip-1", detections, FPS, "job-1")
    types = [e["event_type"] for e in out["events"]]
    assert "snap" in types


# ── Multi-signal path: snap + LOS ────────────────────────────────────────────


def test_multisignal_snap_and_los() -> None:
    out = stage_events.run(
        "clip-2",
        {},
        FPS,
        "job-2",
        tracklets=_formation(fx=30.0),
        pose_by_frame=_ol_pose(),
        ol_track_ids=["p0", "p1", "p2", "p3"],
    )
    assert out["snap_detected"] is True
    snap = next(e for e in out["events"] if e["event_type"] == "snap")
    assert SNAP - 2 <= snap["frame_number"] <= SNAP + 2
    attrs = snap["attributes"]
    assert abs(attrs["los_x"] - 30.0) < 1.0
    assert 0.0 <= attrs["los_confidence"] <= 1.0
    assert "formation_break" in attrs["signals_present"]
    assert attrs["source"] == "model"


def test_multisignal_no_snap_returns_gracefully() -> None:
    # Single motion man among a static squad ⇒ no defensible snap.
    tracks = _formation(fx=30.0)
    for i in range(1, 11):  # freeze everyone but p0
        for pt in tracks[i]["track_points"]:
            pt["field_x"] = 30.0
    out = stage_events.run("clip-3", {}, FPS, "job-3", tracklets=tracks)
    assert out["snap_detected"] is False
    assert out["events"] == []


# ── Multi-signal path: full pass with ball ───────────────────────────────────


def _pass_tracklets() -> list[dict[str, Any]]:
    """QB, 5 OL, and a WR that arrives at the catch point.

    The OL are clustered at the LOS (x≈20) but offset to one side in y so they
    stay clear of the ball's release point — keeping the throw-attribution
    nearest-player rule unambiguous in this fixture.
    """
    tracks: list[dict[str, Any]] = []
    for i in range(5):
        pts = []
        for f in range(0, 25):
            x = 20.0 + (0.0 if f < SNAP else 0.4 * (f - SNAP + 1))
            pts.append({"frame_number": f, "field_x": x, "field_y": float(-5 + i)})
        tracks.append({"id": f"ol{i}", "track_points": pts})
    # QB just behind the LOS, at the release point.
    qb_pts = [{"frame_number": f, "field_x": 19.0, "field_y": 0.0} for f in range(0, 25)]
    # WR split wide, arriving at the catch point (35, 10) as the ball settles.
    wr_pts = []
    for f in range(0, 25):
        frac = min(1.0, max(0.0, (f - SNAP) / 5.0))
        wr_pts.append({"frame_number": f, "field_x": 20.0 + 15.0 * frac,
                       "field_y": 12.0 - 2.0 * frac})
    tracks.append({"id": "qb", "track_points": qb_pts})
    tracks.append({"id": "wr", "track_points": wr_pts})
    return tracks


def _ball_detections() -> dict[str, list[dict[str, Any]]]:
    # Ball (field == pixels via identity homography): at QB until release,
    # then a fast arc settling at the receiver.
    path = {
        8: (17.0, 0.0), 9: (17.0, 0.0), 10: (17.0, 0.0),
        11: (17.5, 0.5),  # release: ball still in the QB's hands
        12: (20.0, 2.0), 13: (24.0, 5.0), 14: (28.0, 7.0),
        15: (32.0, 9.0),
    }
    # Ball settles in the receiver's hands — several frames so the tracker's
    # velocity decays below the catch threshold.
    for f in range(16, 24):
        path[f] = (35.0, 10.0)
    return {
        str(f): [{"class": "ball", "bbox": [x - 1, y - 1, x + 1, y + 1], "confidence": 0.8}]
        for f, (x, y) in path.items()
    }


@pytest.mark.parametrize(
    ("shape", "identity"),
    [
        ("nested", [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]),
        ("flat", [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]),
    ],
)
def test_multisignal_full_pass(shape: str, identity: list[Any]) -> None:
    """Both homography shapes in circulation must behave identically.

    Only ``nested`` was covered before, and the orchestrator passes ``flat`` --
    ``stage_calibrate`` serialises a flat nine-element list. Under the old
    ``np.asarray(h) if h else None`` that became shape ``(9,)`` and the first
    ``H @ [x, y, 1]`` raised, failing the whole events stage. The clip still
    produced a snap from the heuristic-free multisignal path, so the visible
    symptom was only that throws and catches silently stopped existing.
    """
    out = stage_events.run(
        "clip-4",
        {},
        FPS,
        "job-4",
        tracklets=_pass_tracklets(),
        ball_detections=_ball_detections(),
        pose_by_frame=_ol_pose(),
        ol_track_ids=["p0", "p1", "p2", "p3"],
        qb_track_id="qb",
        homography=identity,
    )
    assert out["snap_detected"] is True
    assert out["ball_state_valid"] is True
    types = [e["event_type"] for e in out["events"]]
    assert "snap" in types
    assert "throw" in types
    throw = next(e for e in out["events"] if e["event_type"] == "throw")
    assert throw["attributes"]["qb_id"] == "qb"
    assert "hang_time_s" in throw["attributes"]
    # A completed pass should attribute the catch to the receiver.
    if "catch" in types:
        catch = next(e for e in out["events"] if e["event_type"] == "catch")
        assert catch["attributes"]["receiver_id"] == "wr"
