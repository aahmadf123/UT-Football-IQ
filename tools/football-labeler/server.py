"""Loopback-only staff draft store and original-film viewer. No inference or promotion."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
import mimetypes
import os
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

TOOL = Path(__file__).resolve().parent
LIMIT = 8 * 1024 * 1024
PASSES = {"primary", "secondary", "adjudication"}


class Refusal(ValueError):
    pass


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def validate(draft, source, reviewer, review_pass):
    if not isinstance(draft, dict) or draft.get("source_id") != source["id"]:
        raise Refusal("Draft does not belong to this source.")
    if draft.get("media_sha256") != source.get("media_sha256"):
        raise Refusal(
            "Draft source hash differs from the catalog; reload the matching source."
        )
    if draft.get("reviewer_id") != reviewer or draft.get("review_pass") != review_pass:
        raise Refusal("Draft reviewer and pass must match the open workspace.")
    if (
        draft.get("reviewed") is not False
        or draft.get("evidence_kind") != "annotation_draft"
    ):
        raise Refusal("Only unreviewed annotation drafts can be saved here.")
    if type(draft.get("schema_version")) is not int or draft["schema_version"] != 1:
        raise Refusal("Unsupported draft schema.")
    if not isinstance(draft.get("plays"), list):
        raise Refusal("Draft plays must be a list.")
    ids = set()
    for play in draft["plays"]:
        if (
            not isinstance(play, dict)
            or not isinstance(play.get("id"), str)
            or not play["id"].strip()
            or play["id"] in ids
        ):
            raise Refusal("Each play needs a unique source-local ID.")
        ids.add(play["id"])
        start, end, snap = (
            play.get("start_seconds"),
            play.get("end_seconds"),
            play.get("snap_seconds"),
        )
        if (
            not isinstance(play.get("canonical_play_id"), str)
            or not play["canonical_play_id"].strip()
        ):
            raise Refusal(
                "Each saved play needs a canonical play ID; related views share that ID."
            )
        if (
            not all(number(v) for v in (start, end))
            or not 0 <= start < end <= source["duration_seconds"] + 0.001
        ):
            raise Refusal("Play boundaries must be inside the original source.")
        if snap is not None and (not number(snap) or not start <= snap < end):
            raise Refusal("Snap must be within the play or unknown.")
        if (
            play.get("double_labeled") is not False
            or play.get("adjudicated") is not False
        ):
            raise Refusal(
                "Saving a draft cannot establish independent review or adjudication."
            )
        for field in ("dense_tracks", "identities", "events"):
            if not isinstance(play.get(field, []), list):
                raise Refusal(f"{field} must be a list.")
        targets = {}
        for track in play.get("dense_tracks", []):
            if (
                not isinstance(track, dict)
                or not isinstance(track.get("target_id"), str)
                or not track["target_id"].strip()
                or track["target_id"] in targets
            ):
                raise Refusal("Track IDs must be nonempty and unique in the play.")
            targets[track["target_id"]] = track
            if not isinstance(track.get("points", []), list) or not isinstance(
                track.get("visibility_events", []), list
            ):
                raise Refusal("Track points and visibility records must be lists.")
            if track.get("actor_kind", "unknown") not in {
                "player",
                "referee",
                "unknown",
            } or track.get("side", "unknown") not in {
                "offense",
                "defense",
                "unknown",
                "not_applicable",
            }:
                raise Refusal("Choose a supported observed actor kind and side.")
            if track.get("actor_kind") == "referee" and track.get(
                "side", "unknown"
            ) not in {"unknown", "not_applicable"}:
                raise Refusal("A referee cannot be labeled offense or defense.")
            last = -math.inf
            for point in track.get("points", []):
                if not isinstance(point, dict):
                    raise Refusal("Track points must be observation objects.")
                time, box = point.get("timestamp_seconds"), point.get("bbox")
                if not number(time) or not start <= time <= end or time <= last:
                    raise Refusal(
                        "Track observations need increasing source times inside the play."
                    )
                if (
                    not isinstance(box, list)
                    or len(box) != 4
                    or not all(number(v) for v in box)
                    or not 0 <= box[0] < box[2] <= source["width"]
                    or not 0 <= box[1] < box[3] <= source["height"]
                ):
                    raise Refusal(
                        "Visible boxes must fit the actual source dimensions."
                    )
                if (
                    not isinstance(point.get("shot_id"), str)
                    or not point["shot_id"].strip()
                ):
                    raise Refusal("Each visible observation needs its source shot ID.")
                last = time
            for event in track.get("visibility_events", []):
                if not isinstance(event, dict):
                    raise Refusal("Visibility records must be observation objects.")
                if (
                    event.get("visibility_state") not in {"occluded", "offscreen"}
                    or not number(event.get("timestamp_seconds"))
                    or not start <= event["timestamp_seconds"] <= end
                ):
                    raise Refusal(
                        "Visibility loss needs an observed state and source time."
                    )
        for identity in play.get("identities", []):
            if not isinstance(identity, dict):
                raise Refusal("Identity evidence must be an object.")
            target = targets.get(identity.get("target_id"))
            if (
                target is None
                or type(identity.get("readable")) is not bool
                or type(identity.get("team_resolved")) is not bool
            ):
                raise Refusal(
                    "Identity evidence must reference an existing target and explicit evidence flags."
                )
            if identity.get("player_id") or identity.get("canonical_entity_id"):
                if (
                    target.get("actor_kind") == "referee"
                    or not identity["readable"]
                    or not identity["team_resolved"]
                ):
                    raise Refusal(
                        "Roster identity needs readable player/team evidence; referees have no player roster identity."
                    )
            if identity.get("player_id") and not identity.get("canonical_entity_id"):
                raise Refusal("A known player needs its canonical entity ID.")
        for event in play.get("events", []):
            if not isinstance(event, dict):
                raise Refusal("Event evidence must be an object.")
            if (
                not isinstance(event.get("event_type"), str)
                or not event["event_type"].strip()
                or not number(event.get("timestamp_seconds"))
                or not start <= event["timestamp_seconds"] <= end
            ):
                raise Refusal(
                    "Each event needs an observed type and source time within the play."
                )


class Store:
    def __init__(self, catalog_path, drafts, library=None):
        self.catalog_path = Path(catalog_path).resolve(strict=True)
        self.catalog = json.loads(self.catalog_path.read_text(encoding="utf-8-sig"))
        if library:
            self.catalog["sources"] = [
                s for s in self.catalog["sources"] if s.get("library") == library
            ]
        self.sources = {s["id"]: s for s in self.catalog["sources"]}
        if len(self.sources) != len(self.catalog["sources"]) or not self.sources:
            raise Refusal("Catalog needs unique sources.")
        for source in self.sources.values():
            if not all(
                number(source.get(k)) and source[k] > 0
                for k in ("bytes", "width", "height", "duration_seconds")
            ):
                raise Refusal(
                    "Catalog source dimensions, byte size and duration must be known."
                )
        self.drafts = Path(drafts).resolve()
        self.drafts.mkdir(parents=True, exist_ok=True)
        self.directory_lock = (self.drafts / ".server.lock").open("a+b")
        try:
            if self.directory_lock.seek(0, 2) == 0:
                self.directory_lock.write(b"0")
                self.directory_lock.flush()
            self.directory_lock.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.directory_lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.directory_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.directory_lock.close()
            raise Refusal(
                "Another local server already owns this draft folder. Use that server or a different folder."
            ) from exc
        self.lock = threading.RLock()
        self.token = secrets.token_urlsafe(32)
        self.verified = {}
        self.media_paths = {}

    def identity(self, query):
        source = self.sources.get(query.get("source", [""])[0])
        reviewer, review_pass = (
            query.get("reviewer", [""])[0].strip(),
            query.get("pass", [""])[0],
        )
        if (
            source is None
            or not reviewer
            or len(reviewer) > 200
            or review_pass not in PASSES
        ):
            raise Refusal("Choose a catalog source, reviewer ID and review pass first.")
        key = hashlib.sha256(
            json.dumps([source["id"], reviewer, review_pass]).encode()
        ).hexdigest()
        return source, reviewer, review_pass, self.drafts / key

    def read(self, query):
        *_, directory = self.identity(query)
        path = directory / "current.json"
        with self.lock:
            return (
                json.loads(path.read_text(encoding="utf-8"))
                if path.exists()
                else {"revision": None, "draft": None}
            )

    def save(self, query, payload):
        if not isinstance(payload, dict):
            raise Refusal("Save request must contain a draft and its prior revision.")
        source, reviewer, review_pass, directory = self.identity(query)
        validate(payload.get("draft"), source, reviewer, review_pass)
        self.media(source["id"])
        with self.lock:
            previous = self.read(query)
            if payload.get("revision") != previous["revision"]:
                return None
            revision = hashlib.sha256(
                json.dumps(payload, sort_keys=True, allow_nan=False).encode()
            ).hexdigest()
            result = {"revision": revision, "draft": payload["draft"]}
            encoded = (
                json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
            ).encode()
            directory.mkdir(exist_ok=True)
            historical = directory / f"revision-{revision}.json"
            if not historical.exists():
                with historical.open("xb") as stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
            temporary = directory / f"pending-{secrets.token_hex(8)}.json"
            try:
                with temporary.open("xb") as stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, directory / "current.json")
            finally:
                temporary.unlink(missing_ok=True)
            return result

    def media(self, source_id, candidate=None):
        source = self.sources.get(source_id)
        if source is None:
            raise Refusal("Source was not registered in this package.")
        raw = Path(
            candidate or self.media_paths.get(source_id) or source["path"]
        ).expanduser()
        path = (raw if raw.is_absolute() else self.catalog_path.parent / raw).resolve(
            strict=True
        )
        stat = path.stat()
        signature = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
        if stat.st_size != source["bytes"]:
            raise Refusal(
                "Original film size changed; select the exact catalog recording."
            )
        if source.get("media_sha256") and self.verified.get(source_id) != signature:
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            after = path.stat()
            if (
                signature
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                or digest.hexdigest() != source["media_sha256"]
            ):
                raise Refusal(
                    "Original film changed or its full SHA-256 differs from the catalog."
                )
            self.verified[source_id] = signature
        return path


def create_server(catalog, drafts, port=8030, proposals=None, library=None):
    store = Store(catalog, drafts, library)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # No media paths, reviewer IDs or capability tokens in request logs.

        def reply(self, status, value):
            encoded = json.dumps(value, allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(encoded)

        def trusted(self, write=False):
            allowed = {
                f"127.0.0.1:{self.server.server_port}",
                f"localhost:{self.server.server_port}",
            }
            host = self.headers.get("Host", "")
            origin = self.headers.get("Origin")
            if host not in allowed or (origin and origin != f"http://{host}"):
                raise Refusal("Open the local tool directly on its loopback address.")
            if write and not hmac.compare_digest(
                self.headers.get("X-Labeler-Token", ""), store.token
            ):
                raise Refusal(
                    "Local save session expired; reload this page before saving."
                )

        def do_GET(self):
            try:
                self.trusted()
                url = urlsplit(self.path)
                if url.path == "/api/session":
                    return self.reply(
                        200,
                        {
                            "token": store.token,
                            "catalog": store.catalog,
                            "draft_store": str(store.drafts),
                        },
                    )
                if url.path == "/api/draft":
                    return self.reply(200, store.read(parse_qs(url.query)))
                if url.path == "/api/proposals":
                    source_id = parse_qs(url.query).get("source", [""])[0]
                    if not proposals or source_id not in store.sources:
                        return self.reply(
                            404,
                            {
                                "detail": "No experimental hints are configured for this source."
                            },
                        )
                    # Catalog IDs are not used as raw paths; only a plain filename is accepted.
                    if Path(source_id).name != source_id or any(
                        c
                        not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
                        for c in source_id
                    ):
                        raise Refusal("Unsupported source ID for proposal loading.")
                    path = Path(proposals).resolve() / f"{source_id}.json"
                    if not path.is_file():
                        return self.reply(
                            404,
                            {
                                "detail": "No experimental hints for this source. Draw observations manually."
                            },
                        )
                    if path.stat().st_size > 32 * 1024 * 1024:
                        raise Refusal(
                            "Proposal assignment exceeds 32 MiB; split it into smaller source intervals."
                        )
                    return self.reply(200, json.loads(path.read_text(encoding="utf-8")))
                if url.path == "/api/frames":
                    query = parse_qs(url.query)
                    source_id = query.get("source", [""])[0]
                    if source_id not in store.sources:
                        raise Refusal(
                            "Select a registered source before indexing frames."
                        )
                    start, end = (
                        float(query.get("start", ["nan"])[0]),
                        float(query.get("end", ["nan"])[0]),
                    )
                    if (
                        not all(number(v) for v in (start, end))
                        or not 0 <= start < end
                        or end - start > 30
                    ):
                        raise Refusal(
                            "Choose a native-frame interval of at most 30 source seconds."
                        )
                    import importlib.util
                    import subprocess

                    spec = importlib.util.spec_from_file_location(
                        "labeler_frame_index", TOOL / "index_frames.py"
                    )
                    frame_module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(frame_module)
                    try:
                        result = frame_module.build_index(
                            store.catalog_path,
                            source_id,
                            start,
                            end,
                            store.media(source_id),
                        )
                    except (subprocess.SubprocessError, RuntimeError) as exc:
                        raise Refusal(
                            "Frame inspection failed. Install ffprobe, check source decode and try a shorter interval. No frame times were generated."
                        ) from exc
                    return self.reply(200, result)
                if url.path == "/api/media":
                    path = store.media(parse_qs(url.query).get("source", [""])[0])
                    size, start, end = path.stat().st_size, 0, path.stat().st_size - 1
                    partial = self.headers.get("Range")
                    if partial:
                        import re

                        match = re.fullmatch(r"bytes=(\d+)-(\d*)", partial)
                        if not match:
                            return self.reply(
                                416, {"detail": "Unsupported media byte range."}
                            )
                        start, end = (
                            int(match[1]),
                            min(int(match[2]) if match[2] else end, end),
                        )
                        if not 0 <= start <= end < size:
                            return self.reply(
                                416,
                                {
                                    "detail": "Media byte range is outside this original."
                                },
                            )
                    with path.open("rb") as stream:
                        self.send_response(206 if partial else 200)
                        self.send_header(
                            "Content-Type",
                            mimetypes.guess_type(path.name)[0]
                            or "application/octet-stream",
                        )
                        self.send_header("Content-Length", str(end - start + 1))
                        self.send_header("Accept-Ranges", "bytes")
                        if partial:
                            self.send_header(
                                "Content-Range", f"bytes {start}-{end}/{size}"
                            )
                        self.end_headers()
                        stream.seek(start)
                        remaining = end - start + 1
                        while remaining:
                            data = stream.read(min(1024 * 1024, remaining))
                            if not data:
                                break
                            self.wfile.write(data)
                            remaining -= len(data)
                    return
                name = "index.html" if url.path == "/" else url.path.removeprefix("/")
                if name not in {
                    "index.html",
                    "style.css",
                    "app.mjs",
                    "annotations.mjs",
                    "connection.mjs",
                }:
                    return self.reply(
                        404, {"detail": "This file is not part of the local viewer."}
                    )
                data = (TOOL / name).read_bytes()
                self.send_response(200)
                self.send_header(
                    "Content-Type",
                    "text/javascript"
                    if name.endswith(".mjs")
                    else mimetypes.guess_type(name)[0] or "text/plain",
                )
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except (Refusal, OSError, ValueError, KeyError, TypeError) as exc:
                self.reply(
                    422,
                    {
                        "detail": str(exc)
                        if isinstance(exc, Refusal)
                        else "Local file could not be read. Check this package's catalog and original-film path."
                    },
                )

        def do_PUT(self):
            try:
                self.trusted(write=True)
                url = urlsplit(self.path)
                if url.path not in {"/api/draft", "/api/source-path"}:
                    return self.reply(404, {"detail": "Unknown save action."})
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= LIMIT:
                    raise Refusal(
                        "Draft must be between 1 byte and 8 MiB. Export and split larger assignments."
                    )
                payload = json.loads(self.rfile.read(length))
                if url.path == "/api/source-path":
                    if not isinstance(payload, dict) or not isinstance(
                        payload.get("path"), str
                    ):
                        raise Refusal("Supply the exact original's local full path.")
                    source_id = payload.get("source_id")
                    source = store.sources.get(source_id)
                    if not source or not source.get("media_sha256"):
                        raise Refusal(
                            "A full-hash catalog is required before rebinding a moved original."
                        )
                    # Always rehash the candidate: identical stats cannot borrow the old path's proof.
                    store.verified.pop(source_id, None)
                    path = store.media(source_id, payload["path"].strip().strip('"'))
                    store.media_paths[source_id] = str(path)
                    return self.reply(
                        200,
                        {
                            "source_id": source_id,
                            "path": str(path),
                            "media_sha256": source["media_sha256"],
                        },
                    )
                result = store.save(parse_qs(url.query), payload)
                self.reply(
                    409 if result is None else 200,
                    result
                    or {
                        "detail": "Another tab saved this reviewer/pass. Download your recovery draft, then reload the saved draft before merging."
                    },
                )
            except (Refusal, ValueError, TypeError, KeyError, OSError) as exc:
                self.reply(
                    422,
                    {
                        "detail": str(exc)
                        if isinstance(exc, Refusal)
                        else "Draft could not be saved. Check the draft format and local folder permissions; export a recovery copy."
                    },
                )

    class LocalServer(ThreadingHTTPServer):
        def server_close(self):
            super().server_close()
            store.directory_lock.close()

    try:
        server = LocalServer(("127.0.0.1", port), Handler)
    except OSError:
        store.directory_lock.close()
        raise
    server.store = store
    return server


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", required=True, type=Path)
    parser.add_argument("--drafts", required=True, type=Path)
    parser.add_argument("--port", type=int, default=8030)
    parser.add_argument(
        "--proposals",
        type=Path,
        help="Optional directory of source-ID.json experimental detection hints",
    )
    parser.add_argument(
        "--open",
        action="store_true",
        help="Open the local staff viewer in the default browser",
    )
    parser.add_argument(
        "--library",
        choices=["practice", "game"],
        help="Limit the source selector to one actual catalog library",
    )
    args = parser.parse_args()
    try:
        server = create_server(
            args.catalog, args.drafts, args.port, args.proposals, args.library
        )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(
            2,
            f"Cannot start local labeling: {exc}\nCheck the catalog, writable draft folder and port. If 8030 is occupied, use --port 8031; do not stop another staff session.\n",
        )
    print(
        f"Staff labeling: http://127.0.0.1:{server.server_port} (drafts only; Ctrl+C to stop)",
        flush=True,
    )
    if args.open:
        import webbrowser

        threading.Timer(
            0.5, lambda: webbrowser.open(f"http://127.0.0.1:{server.server_port}")
        ).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
