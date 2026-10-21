import hashlib

import pytest

from pipeline import media_identity
from pipeline.media_identity import FULL_HASH_BYTES, content_identity, registered_source_receipt


def test_small_source_receipt_is_actual_full_content(tmp_path):
    path = tmp_path / "film.mp4"
    path.write_bytes(b"actual source bytes")
    identity = content_identity(path)
    registration = {
        "content_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "size_bytes": path.stat().st_size,
        "storage_uri": "local://raw-video/film.mp4",
    }
    receipt = registered_source_receipt(
        path, identity, "video", registration["storage_uri"], registration
    )
    assert receipt["full_sha256"] == registration["content_sha256"]
    assert receipt["registration_algorithm"] == "full-sha256"
    path.write_bytes(b"mutated source bytes")
    with pytest.raises(ValueError):
        registered_source_receipt(
            path, identity, "video", registration["storage_uri"], registration
        )


def test_large_source_registration_matches_api_sampling_without_becoming_full_hash(tmp_path):
    path = tmp_path / "game.mp4"
    # Sparse fixture exercises the actual 64 MiB transition without storing a model/film.
    with path.open("wb") as source:
        source.write(b"header")
        source.seek(FULL_HASH_BYTES + 100)
        source.write(b"end")
    identity = content_identity(path)
    size = identity["source_bytes"]
    digest = hashlib.sha256(b"football-iq sampled fingerprint v1:")
    digest.update(str(size).encode())
    edge, chunk = 4 * 1024 * 1024, 1024 * 1024
    span = size - 2 * edge - chunk
    ranges = [(0, edge), (size - edge, size)] + [
        (edge + span * (i + 1) // 17, edge + span * (i + 1) // 17 + chunk) for i in range(16)
    ]
    with path.open("rb") as source:
        for start, end in sorted(ranges):
            source.seek(start)
            digest.update(start.to_bytes(8, "big"))
            digest.update(source.read(end - start))
    assert identity["source_registration_fingerprint"] == digest.hexdigest()
    assert identity["source_sha256"] != digest.hexdigest()
    registration = {
        "content_sha256": digest.hexdigest(),
        "size_bytes": size,
        "storage_uri": "s3://raw-video/game.mp4",
    }
    receipt = registered_source_receipt(
        path, identity, "video", registration["storage_uri"], registration
    )
    assert receipt["registration_algorithm"] == "sampled-sha256-v1"
    assert receipt["full_sha256"] == identity["source_sha256"]


@pytest.mark.parametrize("field", ["content_sha256", "size_bytes", "storage_uri"])
def test_foreign_source_cannot_receive_registration_proof(tmp_path, field):
    path = tmp_path / "film"
    path.write_bytes(b"film")
    identity = content_identity(path)
    registration = {
        "content_sha256": identity["source_sha256"],
        "size_bytes": 4,
        "storage_uri": "local://raw-video/film",
    }
    registration[field] = "foreign"
    with pytest.raises(ValueError):
        registered_source_receipt(path, identity, "video", "local://raw-video/film", registration)


@pytest.mark.parametrize("mutated", [False, True])
def test_ctime_only_change_rechecks_actual_bytes(tmp_path, monkeypatch, mutated):
    """POSIX hard links alter ctime; restored mtime must not hide new bytes."""
    path = tmp_path / "film"
    path.write_bytes(b"original film")
    identity = content_identity(path)
    registration = {
        "content_sha256": identity["source_sha256"],
        "size_bytes": identity["source_bytes"],
        "storage_uri": path.as_uri(),
    }
    if mutated:
        path.write_bytes(b"modified film")
    # Exercise the Linux link metadata change on every test platform. The
    # content read remains real, including the same-length mutation case.
    updated_stat = [*identity["source_stat"][:4], identity["source_stat"][4] + 1]
    monkeypatch.setattr(media_identity, "source_stat", lambda _: updated_stat)
    if mutated:
        with pytest.raises(ValueError, match="registered source"):
            registered_source_receipt(path, identity, "video", path.as_uri(), registration)
    else:
        receipt = registered_source_receipt(path, identity, "video", path.as_uri(), registration)
        assert receipt["full_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()


def test_link_lifecycle_preserves_registered_source_receipt(tmp_path):
    """Use actual hard links to cover the substage lifecycle used in Linux CI."""
    path = tmp_path / "film"
    path.write_bytes(b"original film")
    identity = content_identity(path)
    alias = tmp_path / "stage-film"
    alias.hardlink_to(path)
    alias.unlink()
    receipt = registered_source_receipt(path, identity, "video", path.as_uri(), {
        "content_sha256": identity["source_sha256"],
        "size_bytes": identity["source_bytes"],
        "storage_uri": path.as_uri(),
    })
    assert receipt["full_sha256"] == identity["source_sha256"]


def test_unchanged_metadata_does_not_read_full_source_again(tmp_path, monkeypatch):
    path = tmp_path / "film"
    path.write_bytes(b"original film")
    identity = content_identity(path)

    def unexpected_read(_):
        raise AssertionError("An unchanged source should use its original verified hash")

    monkeypatch.setattr(media_identity, "content_identity", unexpected_read)
    receipt = registered_source_receipt(path, identity, "video", path.as_uri(), {
        "content_sha256": identity["source_sha256"],
        "size_bytes": identity["source_bytes"],
        "storage_uri": path.as_uri(),
    })
    assert receipt["full_sha256"] == identity["source_sha256"]
