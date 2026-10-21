"""Verify an actual serving build before recording image-retention history.

Uses public read-only endpoints. No credentials, database writes or restarts.
History is a trusted CI artifact written only after these checks pass.
"""

from __future__ import annotations

import argparse
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

REPOSITORY = "footiq-api-edge-backendcontainer"
TAG = re.compile(r"[a-f0-9]{8,40}\Z")
DIGEST = re.compile(r"sha256:[a-f0-9]{64}\Z")


def is_ready(version, readiness, expected_sha):
    return (isinstance(version, dict) and version.get("git_sha") == expected_sha
            and isinstance(readiness, dict) and readiness.get("status") == "ready"
            and isinstance(readiness.get("checks"), dict)
            and readiness["checks"].get("database") == "ok")


def _probe(base_url, timeout):
    values = []
    for path in ("/api/v1/system/version", "/ready"):
        request = Request(base_url.rstrip("/") + path, headers={"User-Agent": "Football-IQ-release-check"})
        with urlopen(request, timeout=timeout) as response:  # noqa: S310
            # Both endpoints return tiny objects; never ingest unbounded error output.
            values.append(json.loads(response.read(16384)))
    return tuple(values)


def wait_until_ready(base_url, expected_sha, *, timeout_seconds=1200, interval_seconds=10,
                     steady_checks=3, probe=_probe, clock=time.monotonic, sleep=time.sleep):
    if not re.fullmatch(r"[a-f0-9]{40}", expected_sha):
        raise ValueError("Expected a full commit SHA")
    if timeout_seconds <= 0 or interval_seconds <= 0 or steady_checks < 1:
        raise ValueError("Readiness timing and consecutive count must be positive")
    deadline = clock() + timeout_seconds
    steady = 0
    while clock() < deadline:
        try:
            version, readiness = probe(base_url, min(5, max(.1, (deadline - clock()) / 2)))
            steady = steady + 1 if is_ready(version, readiness, expected_sha) else 0
        except (OSError, ValueError, URLError):
            # A rollout can briefly drop connections. Never log response bodies/URLs.
            steady = 0
        if steady >= steady_checks:
            return {"git_sha": expected_sha, "database": "ok", "consecutive_checks": steady,
                    "verified_at_utc": datetime.now(timezone.utc).isoformat()}
        sleep(min(interval_seconds, max(0, deadline - clock())))
    raise TimeoutError("The expected backend build and database did not become steadily ready")


def retention_plan(inventory, current_tag, history, current_digest):
    if not isinstance(current_tag, str) or not TAG.fullmatch(current_tag):
        raise ValueError("Invalid current image tag")
    if not isinstance(current_digest, str) or not DIGEST.fullmatch(current_digest):
        raise ValueError("Invalid current image digest")
    previous = []
    if history is not None:
        if not isinstance(history, dict) or history.get("schema_version") != 2:
            raise ValueError("Unsupported image history")
        previous = history.get("verified_images")
        if not isinstance(previous, list) or any(not isinstance(image, dict)
                or not isinstance(image.get("tag"), str) or not TAG.fullmatch(image["tag"])
                or not isinstance(image.get("digest"), str) or not DIGEST.fullmatch(image["digest"])
                for image in previous):
            raise ValueError("Invalid verified image history")
    if not isinstance(inventory, list):
        raise ValueError("Invalid registry inventory")
    available = set()
    for image in inventory:
        if not isinstance(image, dict):
            raise ValueError("Invalid registry row")
        if image.get("name") != REPOSITORY:
            continue
        tags = image.get("tags")
        if not isinstance(tags, list) or any(not isinstance(t, str) or not TAG.fullmatch(t) for t in tags):
            raise ValueError("Unexpected registry tags; refusing deletion")
        available.update(tags)
    images = []
    seen_digests = set()
    tag_digests = {}
    for image in [{"tag": current_tag, "digest": current_digest}, *previous]:
        if image["tag"] in tag_digests and tag_digests[image["tag"]] != image["digest"]:
            raise ValueError("A tag cannot identify conflicting image digests")
        tag_digests[image["tag"]] = image["digest"]
        if image["digest"] not in seen_digests:
            images.append(image)
            seen_digests.add(image["digest"])
    images = images[:3]
    protected = [image["tag"] for image in images]
    # Preserve aliases recorded for a protected digest as well.
    protected_aliases = {image["tag"] for image in previous if image["digest"] in {i["digest"] for i in images}}
    # Bootstrap never destroys the only rollback. Missing historical images also
    # stop pruning until the full protected set is actually available.
    can_prune = len(set(protected)) == 3 and set(protected).issubset(available)
    return {"schema_version": 2, "verified_images": images,
            "delete_tags": sorted(available - set(protected) - protected_aliases) if can_prune else []}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check")
    check.add_argument("--base-url", required=True)
    check.add_argument("--sha", required=True)
    check.add_argument("--output", type=Path, required=True)
    check.add_argument("--timeout", type=float, default=1200)
    retain = sub.add_parser("retain")
    retain.add_argument("--inventory", type=Path, required=True)
    retain.add_argument("--current-tag", required=True)
    retain.add_argument("--current-digest", required=True)
    retain.add_argument("--history", type=Path)
    retain.add_argument("--readiness", type=Path, required=True)
    retain.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "check":
        if not args.base_url.startswith("https://"):
            parser.error("The deployed API must use HTTPS")
        result = wait_until_ready(args.base_url, args.sha, timeout_seconds=args.timeout)
    else:
        readiness = json.loads(args.readiness.read_text(encoding="utf-8"))
        if (not re.fullmatch(r"[a-f0-9]{40}", str(readiness.get("git_sha", "")))
                or readiness.get("database") != "ok" or readiness.get("consecutive_checks", 0) < 3):
            parser.error("Verified readiness evidence is required before retention")
        history = json.loads(args.history.read_text(encoding="utf-8")) if args.history else None
        result = retention_plan(json.loads(args.inventory.read_text(encoding="utf-8")), args.current_tag, history, args.current_digest)
        result["readiness"] = readiness
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print("Release verification recorded" if args.command == "check" else "Image retention plan recorded")


if __name__ == "__main__":
    main()
