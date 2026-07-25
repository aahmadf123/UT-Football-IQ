"""Tests for the offline SportQA/SportR coverage harness (Issue #168).

Locks the contract that the harness runs with no external data, counts American
football, and explicitly excludes soccer.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from eval.sportqa_sportr_eval import (
    SYNTHETIC_SAMPLE,
    USAGE_MARKER,
    build_report,
    is_american_football,
    is_soccer,
    load_examples_jsonl,
    main,
    render_markdown,
)


def test_american_football_detection_is_explicit() -> None:
    assert is_american_football("American Football")
    assert is_american_football("NFL")
    assert is_american_football("college football")
    # A bare "football" must not be read as American football.
    assert not is_american_football("Football")
    assert not is_american_football("Soccer")


def test_soccer_detection_excludes_association_football() -> None:
    assert is_soccer("Soccer")
    assert is_soccer("Association Football")
    assert is_soccer("football")  # bare football → soccer-ambiguous, excluded
    assert not is_soccer("American Football")


def test_synthetic_report_finds_american_football_and_excludes_soccer() -> None:
    report = build_report(list(SYNTHETIC_SAMPLE))
    assert report.american_football_present
    assert report.american_football >= 4
    assert report.soccer_excluded >= 2
    # American football is covered across multiple modalities (text + image/video).
    assert set(report.af_by_modality) >= {"text", "image", "video"}


def test_loader_is_tolerant_and_offline(tmp_path: Path) -> None:
    rows = [
        {"sport": "American Football", "level": "L2-rules", "question": "Down and distance?"},
        {"Sport": "Soccer", "difficulty": "L1", "Q": "Offside?"},
        {"sport": "American Football", "task": "rule-grounding", "image": "frame_0123.jpg"},
        "not-json",  # skipped
        {"no_sport": "ignored"},  # skipped (no sport field)
    ]
    path = tmp_path / "sample.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")

    examples = load_examples_jsonl(path, "sportqa")
    assert len(examples) == 3
    report = build_report(examples)
    assert report.american_football == 2
    assert report.soccer_excluded == 1
    assert report.af_by_modality["image"] == 1


def test_render_markdown_includes_offline_marker() -> None:
    md = render_markdown(build_report(list(SYNTHETIC_SAMPLE)))
    assert USAGE_MARKER in md
    assert "American-football examples" in md


def test_main_demo_runs_offline(capsys) -> None:
    rc = main(["demo"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "American-football examples" in out
