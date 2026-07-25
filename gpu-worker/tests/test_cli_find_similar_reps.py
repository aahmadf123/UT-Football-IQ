"""Smoke tests for the find_similar_reps CLI (Issue #8)."""

from __future__ import annotations

import json
import os
import sys
from typing import Any

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cli import find_similar_reps as cli_module  # noqa: E402


def test_format_results_table_renders_empty_state() -> None:
    body = {"results": [], "reason": "no production embedding model"}
    out = cli_module.format_results_table(body)
    assert "no production embedding model" in out


def test_format_results_table_lists_results_with_labels() -> None:
    body = {
        "anchor_clip_id": "anchor-uuid",
        "model_version_id": "mv-uuid",
        "model_version_label": "play-embed-clip-vitb32-baseline@1.0",
        "experimental": False,
        "results": [
            {
                "clip_id": "11111111-1111-1111-1111-111111111111",
                "score": 0.912,
                "is_experimental": False,
                "snap_anchor": True,
                "label_data": {
                    "formation": {"toledo": "Trips", "generic": "trips_right"},
                    "coverage": {"generic": "cover_3"},
                },
            },
        ],
    }
    out = cli_module.format_results_table(body)
    assert "play-embed-clip-vitb32-baseline@1.0" in out
    assert "0.912" in out
    assert "formation=trips_right" in out
    assert "coverage=cover_3" in out


def test_find_similar_reps_posts_expected_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    class _FakeResponse:
        def __init__(self, payload: dict[str, Any]) -> None:
            self._payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return self._payload

    class _FakeClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> _FakeClient:
            return self

        def __exit__(self, *exc: Any) -> None:
            return None

        def post(
            self, url: str, *, json: dict[str, Any], headers: dict[str, str]
        ) -> _FakeResponse:
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return _FakeResponse({"results": [], "anchor_clip_id": json["clip_id"]})

    monkeypatch.setattr(cli_module.httpx, "Client", _FakeClient)
    body = cli_module.find_similar_reps(
        "clip-abc",
        backend_url="https://api.example.com/",
        token="t0k",
        k=15,
        formation="trips_right",
    )
    assert captured["url"] == "https://api.example.com/api/v1/search/similar"
    assert captured["headers"]["Authorization"] == "Bearer t0k"
    assert captured["json"]["k"] == 15
    assert captured["json"]["filters"]["formation"] == "trips_right"
    # The default keeps experimental output out of the result list.
    assert captured["json"]["filters"]["include_experimental"] is False
    assert body["anchor_clip_id"] == "clip-abc"


def test_main_requires_token(capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli_module.main(["--clip", "x", "--token", ""])
    assert rc == 2
    err = capsys.readouterr().err
    assert "FOOTBALL_IQ_TOKEN" in err


def test_main_emits_json_when_flag_set(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        cli_module,
        "find_similar_reps",
        lambda *a, **kw: {"results": [], "reason": "no production embedding model"},
    )
    rc = cli_module.main(
        ["--clip", "abc", "--token", "tk", "--json", "--backend-url", "http://x"]
    )
    assert rc == 0
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["reason"] == "no production embedding model"
