"""Pinned model downloads must never replace a good model with corrupt bytes."""

from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path

import pytest

from scripts import install_pretrained


def test_bad_asset_preserves_existing_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "model.pt"
    output.write_bytes(b"previous verified model")
    monkeypatch.setattr(
        install_pretrained.urllib.request, "urlopen", lambda *_args, **_kwargs: io.BytesIO(b"bad")
    )
    with pytest.raises(ValueError, match="checksum mismatch"):
        install_pretrained.install(output)
    assert output.read_bytes() == b"previous verified model"
    assert list(tmp_path.iterdir()) == [output]


def test_verified_asset_installs_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = b"synthetic installer bytes, never model training data"
    monkeypatch.setattr(install_pretrained, "SHA256", hashlib.sha256(fixture).hexdigest())
    monkeypatch.setattr(
        install_pretrained.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: io.BytesIO(fixture),
    )
    output = tmp_path / "models" / "model.pt"
    install_pretrained.install(output)
    assert output.read_bytes() == fixture
    if os.name == "nt":
        assert output.stat().st_mode & 0o200  # Windows chmod exposes the writable bit.
    else:
        assert output.stat().st_mode & 0o777 == 0o644
    assert list(output.parent.iterdir()) == [output]
