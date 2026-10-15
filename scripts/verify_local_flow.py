"""Verify a real local library with real film; never writes to a hosted team API.

This deliberately creates a clearly named verification session in a disposable
local library. It exercises real persistence, CPU inference and exported bytes.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument(
        "--profile", choices=("review", "quick", "detailed"), default="quick"
    )
    args = parser.parse_args()
    status = json.loads((args.data_dir / "runtime/status.json").read_text())
    base = status["api_url"]
    if urlparse(base).hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise SystemExit("Verification requires a disposable loopback library")
    config = json.loads((args.data_dir / "config.json").read_text())
    with httpx.Client(base_url=base, timeout=30, trust_env=False) as client:
        nonce = client.post(
            "/api/v1/local/bootstrap", json={"launcher_key": config["launcher_key"]}
        )
        nonce.raise_for_status()
        login = client.post("/api/v1/local/login", json=nonce.json())
        login.raise_for_status()
        token = login.json()["access_token"]
        client.headers["Authorization"] = "Bearer " + token

        def request(method: str, url: str, **kwargs: object) -> object:
            response = client.request(method, url, **kwargs)
            response.raise_for_status()
            return response.json()

        request(
            "PATCH",
            "/api/v1/settings/system",
            json={"system_config": {"auto_process_on_upload": False}},
        )
        session = request(
            "POST",
            "/api/v1/sessions",
            json={
                "title": "Local end-to-end verification",
                "session_kind": "practice",
                "recorded_at": "2026-04-16T15:00:00Z",
                "library_role": "toledo",
                "notes": "Disposable test library; annotations are test edits, not coaching truth.",
            },
        )
        upload = request(
            "POST", "/api/v1/videos/upload-url", json={"filename": args.input.name}
        )
        with args.input.open("rb") as source:
            response = client.put(
                upload["uploadUrl"],
                content=source,
                headers={"Content-Type": "video/mp4"},
            )
        response.raise_for_status()
        stored = response.json()
        assert stored["finalized"] is True
        video = request(
            "POST",
            "/api/v1/videos",
            json={
                "filename": args.input.name,
                "storage_uri": stored["storageUri"],
                "practice_session_id": session["id"],
                "camera_label": "Drone verification",
                "session_kind": "practice",
                "our_possession": "offense",
                "recorded_at": "2026-04-16T15:00:00Z",
            },
        )
        assert video["width"] > 0 and video["duration_seconds"] > 0
        raw = request(
            "GET",
            "/api/v1/videos/download-url",
            params={"bucket": "raw-video", "key": upload["key"]},
        )
        url = raw["downloadUrl"]
        response = client.get(url, headers={"Range": "bytes=0-1023"})
        assert response.status_code == 206 and len(response.content) == 1024
        print(
            "PASS: durable import, metadata probe and seekable raw playback", flush=True
        )

        job = request(
            "POST",
            "/api/v1/jobs",
            json={
                "video_id": video["id"],
                "job_type": "pipeline",
                "pipeline_mode": "same_session",
                "priority": 10,
                "processing_profile": args.profile,
            },
        )
        replay = request(
            "POST",
            "/api/v1/jobs",
            json={
                "video_id": video["id"],
                "job_type": "pipeline",
                "processing_profile": args.profile,
            },
        )
        assert replay["id"] == job["id"]
        deadline = time.monotonic() + 240
        previous = None
        while time.monotonic() < deadline:
            current = request("GET", f"/api/v1/jobs/{job['id']}")
            if current["status"] != previous:
                print("Processing:", current["status"], flush=True)
                previous = current["status"]
            if current["status"] == "succeeded":
                break
            if current["status"] in {"failed", "cancelled"}:
                raise AssertionError(
                    f"Processing did not finish: {current.get('error_stage')}"
                )
            time.sleep(2)
        else:
            raise AssertionError("CPU processing timed out")
        clips = request("GET", f"/api/v1/videos/{video['id']}/clips")
        assert clips
        clip = clips[0]
        assert "://clips/" in clip["storage_uri"]
        playback = request(
            "POST", "/api/v1/system/storage/resolve", json={"uri": clip["storage_uri"]}
        )
        response = client.get(
            playback["download_url"], headers={"Range": "bytes=0-1023"}
        )
        assert response.status_code == 206 and len(response.content) == 1024
        assert (clip.get("provenance") or {}).get("playback", {}).get(
            "kind"
        ) == "clean_clip"
        overlays = request("GET", f"/api/v1/clips/{clip['id']}/overlays")
        if args.profile != "review":
            assert overlays.get("tracks") or overlays.get("tracklets")
        print(
            "PASS: CPU playback job, persistence and clean clip media"
            if args.profile == "review"
            else "PASS: real CPU worker claim, inference, persistence and overlays",
            flush=True,
        )

        correction = request(
            "POST",
            "/api/v1/corrections",
            json={
                "clip_id": clip["id"],
                "correction_type": "formation_tag",
                "corrected_value": {"formation": "Verification tag"},
                "training_eligible": False,
                "notes": "Workflow test edit; not a training label.",
            },
        )
        corrected = request("GET", f"/api/v1/clips/{clip['id']}")
        assert (
            corrected["label_data"]["offensive_formation"]["formation"]
            == "Verification tag"
        )
        request("PATCH", f"/api/v1/clips/{clip['id']}", json={"is_reviewed": True})
        reviewed = request("GET", f"/api/v1/clips/{clip['id']}")
        assert reviewed["is_reviewed"] is True
        print("PASS: immediate correction and persisted review completion", flush=True)

        report = request(
            "POST",
            "/api/v1/reports",
            json={"format": "json", "practice_session_id": session["id"]},
        )
        for _ in range(30):
            report = request("GET", f"/api/v1/reports/{report['id']}")
            if report["status"] == "succeeded":
                break
            assert report["status"] != "failed"
            time.sleep(1)
        assert report["status"] == "succeeded"
        download = request("GET", f"/api/v1/reports/{report['id']}/download")
        response = client.get(download["download_url"])
        response.raise_for_status()
        assert response.json()
        print(
            "PASS: durable scoped report generation and actual downloadable bytes",
            flush=True,
        )
        if args.evidence:
            args.evidence.write_text(
                json.dumps(
                    {
                        "session_id": session["id"],
                        "video_id": video["id"],
                        "job_id": job["id"],
                        "clip_id": clip["id"],
                        "correction_id": correction["id"],
                        "report_id": report["id"],
                        "source": "real_cpu_inference"
                        if args.profile != "review"
                        else "real_playback_only",
                        "test_annotations_training_eligible": False,
                    },
                    indent=2,
                )
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
