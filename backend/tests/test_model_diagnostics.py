"""Protocol math and storage contracts, never evidence of football accuracy."""

import json
import subprocess
from pathlib import Path

import pytest
from app.services.expected_outcomes import _metrics
from app.services.model_diagnostics import confusion_matrix, outcome_diagnostics


def test_actual_paired_diagnostics_include_abstentions_and_fixed_error_bins():
    matrix = confusion_matrix(
        ["cover_1", "cover_2", "cover_2"], ["cover_1", "cover_1", "__abstained__"]
    )
    assert matrix["labels"] == ["__abstained__", "cover_1", "cover_2"]
    assert matrix["counts"] == [[0, 0, 0], [0, 1, 0], [1, 1, 0]]
    diagnostics = outcome_diagnostics([0, 0, 1, 1], [0.1, 0.7, 0.3, 0.9], True)
    assert diagnostics["confusion_matrix"]["counts"] == [[1, 1], [1, 1]]
    assert sum(b["count"] for b in diagnostics["error_distribution"]["bins"]) == 4
    with pytest.raises(ValueError):
        outcome_diagnostics([0], [float("nan")], True)


def test_ui_fixture_is_actual_saved_protocol_diagnostics_not_hand_authored_accuracy():
    path = (
        Path(__file__).resolve().parents[2]
        / "frontend/src/components/__fixtures__/model-reliability-producer.json"
    )
    fixture = json.loads(path.read_text(encoding="utf-8"))
    assert "not real football validation" in fixture["fixture_notice"]
    actual = _metrics(
        [i % 2 for i in range(20)], [0.98 if i % 2 else 0.02 for i in range(20)], True
    )
    assert fixture["evaluation"]["metrics"] == actual
    assert fixture["evaluation"]["passed"] is False
    assert fixture["evaluation"]["population"]["canonical_plays"] == 20


def test_count_and_schema_identity_comparison_is_exact_while_measured_float_is_tolerant():
    from app.services.expected_outcomes import _close

    for key, expected in [
        ("count", 20),
        ("diagnostics_version", 1),
        ("schema_version", 1),
        ("decision_threshold", 0.5),
    ]:
        assert not _close({key: expected + 1e-9}, {key: expected})
        assert not _close({key: True}, {key: expected})
    assert not _close({"counts": [[20.00000001]]}, {"counts": [[20]]})
    assert _close({"brier": 0.25 + 1e-10}, {"brier": 0.25})


@pytest.mark.parametrize("binary", [True, False])
def test_worker_report_diagnostics_match_independent_server_statistics(worker_python, binary):
    truth = [0, 0, 1, 1] if binary else [-4, 0, 2, 8]
    predicted = [0.1, 0.7, 0.3, 0.9] if binary else [-1, 0, 4, 4]
    code = """
import json,sys,numpy as np
from pipeline.expected_outcomes.model import _metrics
y,p,b=json.loads(sys.argv[1])
print(json.dumps(_metrics(np.array(y),np.array(p),b)))
"""
    result = subprocess.run(  # noqa: S603 -- fixed local worker command and numeric fixtures
        [worker_python, "-c", code, json.dumps([truth, predicted, binary])],
        cwd=Path(__file__).resolve().parents[2] / "gpu-worker",
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    worker = json.loads(result.stdout)
    server = _metrics(truth, predicted, binary)
    for key in ("diagnostics_version", "error_distribution", "confusion_matrix"):
        assert worker.get(key) == server.get(key)
