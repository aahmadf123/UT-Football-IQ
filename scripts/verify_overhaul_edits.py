"""Exercise boundary edits and optional real reprocessing in a disposable local library.

Only loopback libraries are accepted. Uses real previously processed film; test
edits are reverted through the public undo API, retaining their audit history.
Failures attempt reverse-order cleanup without overriding later staff changes.
This verifies application wiring, never football annotation accuracy.
"""
from __future__ import annotations

import argparse
import json
import time
from contextlib import ExitStack
from pathlib import Path
from urllib.parse import urlparse

import httpx


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--workspace-evidence", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--reprocess", action="store_true")
    args = parser.parse_args()
    status = json.loads((args.data_dir / "runtime/status.json").read_text())
    base = status["api_url"]
    if urlparse(base).hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise SystemExit("Only disposable loopback libraries are supported")
    config = json.loads((args.data_dir / "config.json").read_text())
    workspace = json.loads(args.workspace_evidence.read_text())
    checks: dict[str, object] = {"scope": "disposable_local_library", "football_accuracy": "not_evaluated"}
    with httpx.Client(base_url=base, timeout=30, trust_env=False) as client, ExitStack() as cleanup:
        nonce = client.post("/api/v1/local/bootstrap", json={"launcher_key": config["launcher_key"]})
        nonce.raise_for_status()
        login = client.post("/api/v1/local/login", json=nonce.json())
        login.raise_for_status()
        client.headers["Authorization"] = "Bearer " + login.json()["access_token"]

        def request(method: str, path: str, **kwargs: object) -> object:
            response = client.request(method, path, **kwargs)
            if not response.is_success:
                raise AssertionError(f"{method} {path}: HTTP {response.status_code}: {response.text[:400]}")
            return response.json()

        def clips() -> list[dict]:
            return request("GET", f"/api/v1/videos/{workspace['video_id']}/clips")

        def signature(items: list[dict]) -> list[tuple]:
            return sorted((item["id"], item["start_time"], item["end_time"]) for item in items)

        def history(operation: dict, clip_id: str) -> dict:
            return next(item for item in request("GET", f"/api/v1/clips/{clip_id}/edits") if item["operation_id"] == operation["operation_id"])

        def undo(operation: dict) -> dict:
            assert operation["undo_available"]
            return request("POST", f"/api/v1/clip-edits/{operation['operation_id']}/undo", json={"expected_revisions": operation["revisions"]})

        def register_cleanup(operation: dict) -> None:
            def restore() -> None:
                try:
                    current = history(operation, operation["clips"][0]["id"])
                    if current.get("state") == "undone":
                        return
                    if not current["undo_available"]:
                        print("Cleanup retained an edit because later changes prevent safe undo:", operation["operation_id"], flush=True)
                        return
                    undo(current)
                except Exception as cause:
                    print("Cleanup could not undo an edit in the disposable library:", operation["operation_id"], str(cause)[:200], flush=True)
            cleanup.callback(restore)

        baseline = clips()
        original = next(item for item in baseline if item["id"] == workspace["clip_id"])
        split = request("POST", f"/api/v1/clips/{original['id']}/split", json={"at_seconds": (original["start_time"] + original["end_time"]) / 2, "expected_revision": original.get("annotation_revision", 0)})
        register_cleanup(split)
        assert len(split["clips"]) == 2
        preserved = request("GET", f"/api/v1/clips/{original['id']}")
        assert preserved["provenance"]["state"] == "superseded"
        assert original["id"] not in {item["id"] for item in clips()}
        children = split["clips"]
        merge = request("POST", "/api/v1/clips/merge", json={"clip_ids": [item["id"] for item in children], "expected_revisions": {item["id"]: item["annotation_revision"] for item in children}})
        register_cleanup(merge)
        assert len(merge["clips"]) == 1
        assert merge["clips"][0]["start_time"] == original["start_time"]
        assert merge["clips"][0]["end_time"] == original["end_time"]
        undo(merge)
        current_split = history(split, children[0]["id"])
        assert current_split["undo_available"], "Reverting a descendant structural edit must allow parent undo"
        checks["nested_split_merge_reverse_undo"] = True
        checks["original_deep_link_preserved"] = True
        if args.reprocess:
            expected = signature(clips())
            job = request("POST", "/api/v1/jobs", json={"video_id": workspace["video_id"], "job_type": "pipeline", "pipeline_mode": "same_session", "priority": 10, "processing_profile": "quick"})
            deadline = time.monotonic() + 300
            previous = None
            while time.monotonic() < deadline:
                current = request("GET", f"/api/v1/jobs/{job['id']}")
                if current["status"] != previous:
                    print("Reprocessing:", current["status"], flush=True)
                    previous = current["status"]
                if current["status"] == "succeeded":
                    break
                assert current["status"] not in {"failed", "cancelled"}, current.get("error_stage")
                time.sleep(2)
            else:
                raise AssertionError("Real reprocessing exceeded five minutes")
            assert signature(clips()) == expected, "Reprocessing changed staff boundaries or IDs"
            overrides = request("GET", f"/api/v1/videos/{workspace['video_id']}/boundary-overrides")
            assert overrides["active"] and signature(overrides["clips"]) == expected
            checks["real_reprocessing_preserves_staff_ids_and_windows"] = True
            checks["reprocessing_job_id"] = job["id"]
            current_split = history(split, children[0]["id"])
        undo(current_split)
        assert signature(clips()) == signature(baseline)
        restored = request("GET", f"/api/v1/clips/{original['id']}")
        def labels(item: dict) -> dict:
            return {key: value for key, value in (item.get("label_data") or {}).items()
                    if key != "_annotation_revision"}
        assert labels(restored) == labels(original), "Original staff labels were lost"
        checks["restored_original_boundaries_and_labels"] = True
        checks["audit_operations_retained"] = len(request("GET", f"/api/v1/clips/{original['id']}/edits"))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(checks, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"result": "passed", "evidence": str(args.output)}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
