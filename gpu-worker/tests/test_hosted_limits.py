"""Hosted jobs must leave enough scratch space for the shared API process."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from pipeline import hosted_limits


def test_source_preflight_rejects_large_or_unverified_files() -> None:
    assert "512 MiB" in (hosted_limits.source_limit_reason(2**30, 8 * 2**30) or "")
    assert "unverified" in (hosted_limits.source_limit_reason(None, 8 * 2**30) or "")
    assert "disk space" in (hosted_limits.source_limit_reason(1024, 2**30) or "")
    assert hosted_limits.source_limit_reason(1024, 5 * 2**30) is None


def test_master_is_probed_before_large_stage_copies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PIPELINE_HOSTED", "1")
    monkeypatch.setattr(hosted_limits, "check_source", lambda _size: None)
    monkeypatch.setattr(
        hosted_limits.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            stdout=json.dumps(
                {"format": {"duration": "301"}, "streams": [{"width": 1920, "height": 1080}]}
            )
        ),
    )
    source = tmp_path / "synthetic-test-media.mp4"
    source.write_bytes(b"synthetic probe fixture, not real film")
    with pytest.raises(hosted_limits.HostedCapacityError, match="5 minutes"):
        hosted_limits.validate_master(source)


def test_local_workers_keep_their_existing_media_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PIPELINE_HOSTED", raising=False)
    hosted_limits.validate_master(tmp_path / "not-downloaded.mp4")


def test_hosted_worker_releases_completed_checkpoints_only_when_hosted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = tmp_path / "video-key"
    ledger.mkdir()
    (ledger / "detect.json").write_text("{}")
    monkeypatch.delenv("PIPELINE_HOSTED", raising=False)
    hosted_limits.release_completed_checkpoints(ledger)
    assert ledger.is_dir()
    monkeypatch.setenv("PIPELINE_HOSTED", "1")
    hosted_limits.release_completed_checkpoints(ledger)
    assert not ledger.exists()
