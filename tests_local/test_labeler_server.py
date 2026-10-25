"""Actual isolated loopback persistence tests; fixture bytes are not football labels."""

import copy
import hashlib
import importlib.util
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "staff_labeler_server",
    Path(__file__).resolve().parents[1] / "tools/football-labeler/server.py",
)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


@pytest.fixture
def workspace(tmp_path):
    media = tmp_path / "protocol.mp4"
    media.write_bytes(b"synthetic transport bytes only")
    source = {
        "id": "protocol-source",
        "filename": media.name,
        "path": str(media),
        "bytes": media.stat().st_size,
        "width": 100,
        "height": 80,
        "duration_seconds": 4,
        "media_sha256": hashlib.sha256(media.read_bytes()).hexdigest(),
    }
    catalog = tmp_path / "sources.json"
    catalog.write_text(json.dumps({"schema_version": 1, "sources": [source]}))
    server = module.create_server(catalog, tmp_path / "saved", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"

    def request(path, body=None, token=True, headers=None):
        merged = headers or {}
        if token:
            merged = {**merged, "X-Labeler-Token": server.store.token}
        req = urllib.request.Request(
            base + path,
            data=json.dumps(body).encode() if body is not None else None,
            method="PUT" if body is not None else "GET",
            headers=merged,
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()

    yield server, source, media, request
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def draft(source):
    return {
        "schema_version": 1,
        "evidence_kind": "annotation_draft",
        "reviewed": False,
        "source_id": source["id"],
        "media_sha256": source["media_sha256"],
        "reviewer_id": "staff-a",
        "review_pass": "primary",
        "plays": [
            {
                "id": "p1",
                "canonical_play_id": "canonical-p1",
                "start_seconds": 0,
                "snap_seconds": 1,
                "end_seconds": 3,
                "double_labeled": False,
                "adjudicated": False,
                "practice_period": "inside_run",
                "offense_concept": "staff observed only",
                "defense_concept": None,
                "dense_tracks": [],
                "identities": [],
                "events": [],
            }
        ],
    }


QUERY = "/api/draft?source=protocol-source&reviewer=staff-a&pass=primary"


def test_actual_save_reload_history_conflict_and_pass_isolation(workspace):
    server, source, media, request = workspace
    original = media.read_bytes()
    value = draft(source)
    code, raw = request(QUERY, {"revision": None, "draft": value})
    assert code == 200
    revision = json.loads(raw)["revision"]
    assert json.loads(request(QUERY)[1])["draft"] == value
    changed = copy.deepcopy(value)
    changed["plays"][0]["notes"] = "Actual test edit"
    assert request(QUERY, {"revision": None, "draft": changed})[0] == 409
    assert request(QUERY, {"revision": revision, "draft": changed})[0] == 200
    assert len(list(server.store.drafts.rglob("revision-*.json"))) == 2
    assert (
        json.loads(request(QUERY.replace("primary", "secondary"))[1])["draft"] is None
    )
    assert media.read_bytes() == original
    assert (
        json.loads(next(server.store.drafts.rglob("revision-*.json")).read_text())[
            "draft"
        ]["reviewed"]
        is False
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "foreign_source",
        "foreign_hash",
        "reviewed",
        "invalid_box",
        "referee_side",
        "referee_identity",
        "bad_structure",
    ],
)
def test_invalid_evidence_is_not_saved(workspace, mutation):
    server, source, _, request = workspace
    value = draft(source)
    track = {
        "target_id": "ref",
        "actor_kind": "referee",
        "side": "not_applicable",
        "points": [
            {"timestamp_seconds": 1, "shot_id": "observed-shot", "bbox": [1, 1, 10, 10]}
        ],
    }
    if mutation == "foreign_source":
        value["source_id"] = "other"
    if mutation == "foreign_hash":
        value["media_sha256"] = "b" * 64
    if mutation == "reviewed":
        value["reviewed"] = True
    if mutation == "invalid_box":
        track["points"][0]["bbox"] = [0, 0, 200, 10]
    if mutation == "referee_side":
        track["side"] = "offense"
    if mutation == "referee_identity":
        value["plays"][0]["identities"] = [
            {
                "target_id": "ref",
                "player_id": "roster",
                "canonical_entity_id": "entity",
                "readable": True,
                "team_resolved": True,
            }
        ]
    if mutation == "bad_structure":
        track["points"] = [True]
    value["plays"][0]["dense_tracks"] = [track]
    assert request(QUERY, {"revision": None, "draft": value})[0] == 422
    assert not list(server.store.drafts.rglob("current.json"))


def test_source_bytes_ranges_and_local_write_capability(workspace):
    _, source, media, request = workspace
    code, data = request(
        "/api/media?source=protocol-source", headers={"Range": "bytes=2-8"}
    )
    assert code == 206 and data == media.read_bytes()[2:9]
    assert request("/api/media?source=../outside")[0] == 422
    assert request("/server.py")[0] == 404
    assert (
        request(QUERY, {"revision": None, "draft": draft(source)}, token=False)[0]
        == 422
    )
    assert (
        request(
            QUERY,
            {"revision": None, "draft": draft(source)},
            headers={"Origin": "https://foreign.invalid"},
        )[0]
        == 422
    )
    assert request("/api/session", headers={"Host": "foreign.invalid"})[0] == 422
    media.write_bytes(b"x" * source["bytes"])
    assert request("/api/media?source=protocol-source")[0] == 422
    assert request(QUERY, {"revision": None, "draft": draft(source)})[0] == 422


def test_second_server_cannot_share_a_draft_directory(workspace):
    server, _, _, _ = workspace
    with pytest.raises(module.Refusal, match="already owns"):
        module.create_server(server.store.catalog_path, server.store.drafts, 0)


def test_moved_source_path_requires_actual_full_identity(workspace, tmp_path):
    _, source, media, request = workspace
    moved = tmp_path / "moved-original.mp4"
    moved.write_bytes(media.read_bytes())
    code, raw = request(
        "/api/source-path", {"source_id": source["id"], "path": str(moved)}
    )
    assert code == 200 and json.loads(raw)["media_sha256"] == source["media_sha256"]
    media.unlink()
    assert request("/api/media?source=protocol-source")[0] == 200
    assert request(QUERY, {"revision": None, "draft": draft(source)})[0] == 200
    impostor = tmp_path / "different.mp4"
    impostor.write_bytes(b"z" * source["bytes"])
    assert (
        request("/api/source-path", {"source_id": source["id"], "path": str(impostor)})[
            0
        ]
        == 422
    )
    assert request("/api/media?source=protocol-source")[1] == moved.read_bytes()
