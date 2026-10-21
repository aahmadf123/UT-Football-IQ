"""A local "download" is a hard link: no bytes written, source untouched."""

import os
from pathlib import Path

from pipeline.storage import _copy_to_temp


def test_local_source_is_linked_not_copied(tmp_path: Path) -> None:
    source = tmp_path / "game.mp4"
    source.write_bytes(b"x" * 4096)
    linked = _copy_to_temp(source, "game")
    try:
        assert linked.read_bytes() == source.read_bytes()
        assert os.stat(source).st_nlink == 2  # one file, two names
    finally:
        linked.unlink()
    assert source.exists() and os.stat(source).st_nlink == 1


def test_falls_back_to_copy_when_links_fail(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "game.mp4"
    source.write_bytes(b"abc")

    def no_links(*_args: object) -> None:
        raise OSError("cross-device link")

    monkeypatch.setattr("pipeline.storage.os.link", no_links)
    copied = _copy_to_temp(source, "game")
    try:
        assert copied.read_bytes() == b"abc" and os.stat(source).st_nlink == 1
    finally:
        copied.unlink()
