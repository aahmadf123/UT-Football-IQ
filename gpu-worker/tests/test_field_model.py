"""Field detector artifact-cache tests."""

from __future__ import annotations

from pipeline import field_model


def test_detector_cache_invalidates_when_weights_change(monkeypatch, tmp_path) -> None:
    weights = tmp_path / "field.pt"
    weights.write_bytes(b"first weights")
    monkeypatch.setenv("MODEL_FIELD_PATH", str(weights))
    monkeypatch.setattr(field_model, "_cached", None)
    loaded = []

    def detector(path):
        instance = object()
        loaded.append((path, instance))
        return instance

    monkeypatch.setattr(field_model, "FieldNumberDetector", detector)
    first = field_model.get_field_detector()
    weights.write_bytes(b"replacement weights")
    second = field_model.get_field_detector()

    assert first is loaded[0][1]
    assert second is loaded[1][1]
    assert first is not second


def test_detector_cache_reuses_unchanged_weights(monkeypatch, tmp_path) -> None:
    weights = tmp_path / "field.pt"
    weights.write_bytes(b"same weights")
    monkeypatch.setenv("MODEL_FIELD_PATH", str(weights))
    monkeypatch.setattr(field_model, "_cached", None)
    loaded = []
    monkeypatch.setattr(
        field_model,
        "FieldNumberDetector",
        lambda path: loaded.append(object()) or loaded[-1],
    )

    first = field_model.get_field_detector()
    second = field_model.get_field_detector()

    assert first is second
    assert len(loaded) == 1
