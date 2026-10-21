"""Verify new workspace contracts against a disposable, running loopback library.

Uses actual processed film already in the library; creates only a named test
saved view and report. Credentials and signed URLs never enter the evidence.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    status = json.loads((args.data_dir / "runtime/status.json").read_text())
    base = status["api_url"]
    if urlparse(base).hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("Only disposable loopback libraries are supported")
    config = json.loads((args.data_dir / "config.json").read_text())
    checks: dict[str, object] = {"scope": "disposable_local_library", "football_accuracy": "not_evaluated"}
    with httpx.Client(base_url=base, timeout=30, trust_env=False) as client:
        nonce = client.post("/api/v1/local/bootstrap", json={"launcher_key": config["launcher_key"]})
        nonce.raise_for_status()
        login = client.post("/api/v1/local/login", json=nonce.json())
        login.raise_for_status()
        client.headers["Authorization"] = "Bearer " + login.json()["access_token"]

        def request(method: str, path: str, **kwargs: object) -> object:
            response = client.request(method, path, **kwargs)
            response.raise_for_status()
            return response.json() if response.status_code != 204 else None

        definitions = request("GET", "/api/v1/metrics/definitions")["definitions"]
        assert definitions and all(item["version"] and item["required_evidence"] for item in definitions)
        checks["metric_definitions"] = len(definitions)
        query = request("POST", "/api/v1/analytics/query", json={"session_kind": "practice"})
        assert query["dataset_version"] and "unknown_count" in query["cohort"]
        checks["cohort"] = query["cohort"]
        view = request("POST", "/api/v1/saved-views", json={"name": "Disposable workflow verification", "surface": "analytics", "filters": {"session_kind": "practice", "position": "WR", "layout": "analyst"}})
        assert any(item["id"] == view["id"] for item in request("GET", "/api/v1/saved-views"))
        updated = request("PATCH", f"/api/v1/saved-views/{view['id']}", json={"name": "Renamed verification", "visibility": "team"})
        assert updated["name"] == "Renamed verification" and updated["can_edit"]
        request("DELETE", f"/api/v1/saved-views/{view['id']}")
        assert all(item["id"] != view["id"] for item in request("GET", "/api/v1/saved-views"))
        checks["saved_view_create_read_update_delete"] = True
        videos = request("GET", "/api/v1/videos")
        assert videos
        video = videos[0]
        clips = request("GET", f"/api/v1/videos/{video['id']}/clips")
        assert clips
        clip = clips[0]
        evidence = request("GET", f"/api/v1/plays/{clip.get('canonical_play_id') or clip['id']}/evidence")
        assert evidence["views"] and evidence["readiness"]["film"]
        checks["play_evidence"] = {"play_id": evidence["play_id"], "readiness": evidence["readiness"]}
        checks["video_id"] = video["id"]
        checks["clip_id"] = clip["id"]
        playback = request("POST", "/api/v1/system/storage/resolve", json={"uri": clip["storage_uri"]})
        media = client.get(playback["download_url"])
        media.raise_for_status()
        assert media.content
        checks["proxy_sha256"] = hashlib.sha256(media.content).hexdigest()
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            raise AssertionError("ffprobe required for actual derivative decode verification")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "proxy.mp4"
            path.write_bytes(media.content)
            measured = subprocess.run([ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)], check=True, capture_output=True, text=True)
            probe = json.loads(measured.stdout)
            stream = next(item for item in probe["streams"] if item["codec_type"] == "video")
            numerator, denominator = map(float, stream["avg_frame_rate"].split("/"))
            fps = numerator / denominator
            duration = float(stream.get("duration") or probe["format"]["duration"])
            assert abs(duration - (clip["end_time"] - clip["start_time"])) <= 1 / fps + 0.001
            ffmpeg = shutil.which("ffmpeg")
            if not ffmpeg:
                raise AssertionError("ffmpeg required to decode the returned proxy")
            subprocess.run([ffmpeg, "-v", "error", "-i", str(path), "-f", "null", "-"], check=True, capture_output=True, timeout=120)
            checks["decoded_proxy"] = {"duration_seconds": duration, "fps": fps, "audio_streams": sum(item["codec_type"] == "audio" for item in probe["streams"]), "all_frames_decoded": True}
        report = request("POST", "/api/v1/reports", json={"format": "json", "video_id": video["id"]})
        for _ in range(60):
            report = request("GET", f"/api/v1/reports/{report['id']}")
            if report["status"] == "succeeded":
                break
            assert report["status"] != "failed", report.get("error_message")
            time.sleep(0.5)
        assert report["status"] == "succeeded"
        download = request("GET", f"/api/v1/reports/{report['id']}/download")
        body = client.get(download["download_url"])
        body.raise_for_status()
        assert body.json()
        checks["durable_report_download"] = True
        inventory = request("GET", "/api/v1/system/release-inventory")
        checks["release_inventory_available"] = bool(inventory)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(checks, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"result": "passed", "evidence": str(args.output), "football_accuracy": "not_evaluated"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
