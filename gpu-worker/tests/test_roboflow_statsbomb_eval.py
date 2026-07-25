"""Tests for the offline Roboflow/StatsBomb-AMF coverage harness (Issue #167).

Locks the contract that the harness runs with no external data and no API key,
maps Roboflow class lists to Football-IQ classes, excludes soccer, hard-flags
any face/biometric class, and reports StatsBomb AMF data-layer coverage.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from eval.roboflow_statsbomb_eval import (
    SYNTHETIC_ROBOFLOW,
    SYNTHETIC_STATSBOMB_LAYERS,
    USAGE_MARKER,
    build_roboflow_report,
    build_statsbomb_report,
    is_face_biometric_label,
    is_soccer_label,
    load_roboflow_jsonl,
    main,
    matched_football_iq_class,
    render_markdown,
)


def test_class_mapping_to_football_iq_classes() -> None:
    assert matched_football_iq_class("player") == "player"
    assert matched_football_iq_class("Helmet") == "helmet"
    assert matched_football_iq_class("referee") == "official"
    assert matched_football_iq_class("ball") == "ball"
    assert matched_football_iq_class("away team") == "team"
    assert matched_football_iq_class("scoreboard") is None


def test_soccer_labels_are_flagged() -> None:
    assert is_soccer_label("goalkeeper")
    assert is_soccer_label("Goal Post")
    assert is_soccer_label("soccer")
    # A bare "ball"/"football" object class is not soccer.
    assert not is_soccer_label("ball")
    assert not is_soccer_label("football")


def test_face_biometric_labels_are_hard_flagged() -> None:
    assert is_face_biometric_label("face")
    assert is_face_biometric_label("Facial Recognition")
    assert is_face_biometric_label("identity")
    assert not is_face_biometric_label("player")
    assert not is_face_biometric_label("helmet")


def test_synthetic_roboflow_report_covers_classes_and_flags_risks() -> None:
    report = build_roboflow_report(list(SYNTHETIC_ROBOFLOW))
    # players, ball, helmet and officials are all covered by the sample.
    assert {"player", "ball", "helmet", "official"} <= report.covered_classes
    # The soccer-mislabeled and face-id datasets are flagged.
    assert report.has_soccer
    assert report.has_face_biometric
    # Perspective is tracked so we can tell broadcast/NFL from single-camera.
    assert sum(report.by_perspective.values()) == len(SYNTHETIC_ROBOFLOW)


def test_roboflow_loader_is_tolerant_and_offline(tmp_path: Path) -> None:
    rows = [
        {"name": "a", "classes": ["player", "helmet", "ball"], "perspective": "broadcast"},
        {"name": "b", "names": {"0": "player", "1": "referee"}},  # YOLO names mapping
        "not-json",  # skipped
        {"name": "c", "no_classes": True},  # skipped (no class list)
    ]
    path = tmp_path / "roboflow.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")

    datasets = load_roboflow_jsonl(path)
    assert len(datasets) == 2
    report = build_roboflow_report(datasets)
    assert {"player", "helmet", "ball", "official"} <= report.covered_classes


def test_statsbomb_report_requires_tracking_for_route_coverage() -> None:
    full = build_statsbomb_report(list(SYNTHETIC_STATSBOMB_LAYERS))
    assert full.supports_route_coverage_research
    assert set(full.layers_present) == {"plays", "events", "lft", "tracking"}

    partial = build_statsbomb_report(["plays", "events"])
    assert not partial.supports_route_coverage_research

    unknown = build_statsbomb_report(["plays", "weather"])
    assert unknown.unknown_layers == ("weather",)


def test_render_markdown_includes_offline_marker_and_hard_flag() -> None:
    md = render_markdown(
        build_roboflow_report(list(SYNTHETIC_ROBOFLOW)),
        build_statsbomb_report(list(SYNTHETIC_STATSBOMB_LAYERS)),
    )
    assert USAGE_MARKER in md
    assert "HARD FLAG" in md
    assert "StatsBomb AMF" in md


def test_main_demo_runs_offline(capsys) -> None:
    rc = main(["demo"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Roboflow" in out
    assert "StatsBomb AMF" in out
