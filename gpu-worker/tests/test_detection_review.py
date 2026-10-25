import json
import sys

import pytest

from pipeline.detection_review import proposal_document
from pipeline.video_timeline import VideoTimeline


def test_native_source_bound_proposals_never_invent_names_or_units():
    source = {"id": "source-a", "media_sha256": "a" * 64, "width": 1280, "height": 720}
    result = proposal_document(
        source, {"source_sha256": "a" * 64}, VideoTimeline((0., .031, .071), .1, 30),
        {"2": [{"class": "official", "bbox": [0, 1, 2, 3], "confidence": .8}]},
        model="actual-model", artifact_sha256="b" * 64,
    )
    assert result["frames"][0]["timestamp_seconds"] == .071
    candidate = result["frames"][0]["detections"][0]
    assert candidate["actor_kind"] == "referee"
    assert "player_id" not in candidate and "side" not in candidate
    assert result["provenance"]["review_status"] == "unreviewed"


@pytest.mark.parametrize("verified,sha", [(False, "a" * 64), (True, "c" * 64)])
def test_unknown_clock_or_wrong_source_refused(verified, sha):
    with pytest.raises(ValueError):
        proposal_document(
            {"id": "a", "media_sha256": sha, "width": 1280, "height": 720},
            {"source_sha256": "a" * 64}, VideoTimeline((0.,), .03, 30, verified=verified), {},
            model="actual-model", artifact_sha256="b" * 64,
        )


@pytest.mark.parametrize("frame", ["-1", "1", "999"])
def test_terminal_sentinel_or_out_of_bounds_is_not_an_observed_frame(frame):
    with pytest.raises(ValueError, match="displayed native frame"):
        proposal_document(
            {"id": "a", "media_sha256": "a" * 64, "width": 1280, "height": 720},
            {"source_sha256": "a" * 64}, VideoTimeline((0.,), .03, 30), {frame: []},
            model="actual-model", artifact_sha256="b" * 64,
        )


@pytest.mark.parametrize("ids", [["../draft"], ["inventory"], ["Inventory"], ["same", "SAME"]])
def test_catalog_cannot_escape_or_overwrite_proposals(tmp_path, monkeypatch, ids):
    from pipeline import detection_review

    weights = tmp_path / "cached.pt"
    weights.write_bytes(b"fixture-path-only-no-model-load")
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"sources": [{"id": sid, "library": "practice"} for sid in ids]}))
    output = tmp_path / "output"
    monkeypatch.setattr(sys, "argv", ["review", "--catalog", str(catalog), "--weights", str(weights), "--output", str(output)])
    with pytest.raises(SystemExit) as error:
        detection_review.main()
    assert error.value.code == 2
    assert not output.exists()
