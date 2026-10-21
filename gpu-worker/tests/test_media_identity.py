import hashlib

import pytest

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
