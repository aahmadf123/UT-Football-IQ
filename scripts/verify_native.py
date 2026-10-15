"""Verify a frozen distribution with a new owned PostgreSQL library.

No Python, Docker, Node, R2 keys or model downloads are required by the app.
The verifier's Python/httpx belong only to the build machine. Its clearly marked
test film validates plumbing and playback, never football detection accuracy.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx


def wait_ready(directory: Path, process: subprocess.Popen[bytes]) -> str:
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"Packaged app exited; diagnostics: {directory / 'logs'}"
            )
        status = directory / "runtime/status.json"
        if status.is_file():
            base = json.loads(status.read_text())["api_url"]
            try:
                with httpx.Client(timeout=2, trust_env=False) as client:
                    if client.get(base + "/ready").status_code == 200:
                        return base
            except httpx.HTTPError:
                pass
        time.sleep(0.5)
    raise RuntimeError(
        f"Packaged app startup timed out; diagnostics: {directory / 'logs'}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument(
        "--input",
        type=Path,
        help="Optional real footage for a packaged inference check",
    )
    parser.add_argument(
        "--model",
        type=Path,
        help="Licensed existing player weights; never added to the bundle",
    )
    args = parser.parse_args()
    bundle, data = args.bundle.resolve(), args.data_dir.resolve()
    if data.exists() and any(data.iterdir()):
        raise SystemExit("Native verification requires a new empty disposable library")
    data.mkdir(parents=True, exist_ok=True)
    suffix = ".exe" if os.name == "nt" else ""
    executable = bundle / "launcher/FootballIQ" / ("FootballIQ" + suffix)
    environment = os.environ.copy()
    environment["FOOTBALL_IQ_BUNDLE_ROOT"] = str(bundle)
    profile = "quick" if args.model else "review"
    if args.model:
        if not args.input or not args.model.is_file():
            raise SystemExit(
                "Packaged inference verification needs real footage and existing model weights"
            )
        import shutil

        subprocess.run(
            [str(executable), "doctor", "--data-dir", str(data)],
            cwd=bundle,
            env=environment,
            stdout=subprocess.DEVNULL,
            check=True,
        )
        shutil.copy2(args.model, data / "models/yolov8n.pt")
    ffmpeg = bundle / "native/bin" / ("ffmpeg" + suffix)
    sample = (
        args.input.resolve()
        if args.input
        else data.with_name(data.name + "-verification-film.mp4")
    )
    if not args.input:
        subprocess.run(
            [
                str(ffmpeg),
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=320x180:rate=15",
                "-t",
                "5",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(sample),
            ],
            check=True,
        )
    arguments = [
        str(executable),
        "start",
        "--data-dir",
        str(data),
        "--no-browser",
        "--skip-model-install",
    ]

    def launch() -> subprocess.Popen[bytes]:
        options = (
            {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
            if os.name == "nt"
            else {"start_new_session": True}
        )
        with data.with_name(data.name + "-launcher-output.log").open("ab") as log:
            return subprocess.Popen(
                arguments,
                cwd=bundle,
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
                **options,
            )

    def stop(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is None:
            controls = data / "runtime/controls"
            controls.mkdir(parents=True, exist_ok=True)
            (controls / "verification-stop.json").write_text(
                json.dumps({"action": "shutdown"})
            )
            try:
                process.wait(timeout=45)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=15)
                raise RuntimeError("Packaged app did not shut down gracefully")

    process = launch()
    try:
        base = wait_ready(data, process)
        subprocess.run(
            [
                sys.executable,
                str(Path(__file__).with_name("verify_local_flow.py")),
                "--data-dir",
                str(data),
                "--input",
                str(sample),
                "--profile",
                profile,
                "--evidence",
                str(data / "verification-evidence.json"),
            ],
            check=True,
        )
        state = json.loads((data / "runtime/status.json").read_text())
        worker = next(
            service for service in state["services"] if service["name"] == "worker"
        )
        previous_pid = worker["pid"]
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(previous_pid), "/T", "/F"],
                capture_output=True,
                check=True,
            )
        else:
            os.kill(previous_pid, signal.SIGKILL)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            state = json.loads((data / "runtime/status.json").read_text())
            worker = next(
                service for service in state["services"] if service["name"] == "worker"
            )
            if worker["pid"] and worker["pid"] != previous_pid:
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("Packaged worker did not recover after process loss")
        print(
            "PASS: packaged worker recovered after a real process termination",
            flush=True,
        )
        api = next(service for service in state["services"] if service["name"] == "api")
        previous_api_pid = api["pid"]
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(previous_api_pid), "/T", "/F"],
                capture_output=True,
                check=True,
            )
        else:
            os.kill(previous_api_pid, signal.SIGKILL)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            state = json.loads((data / "runtime/status.json").read_text())
            api = next(
                service for service in state["services"] if service["name"] == "api"
            )
            if api["pid"] and api["pid"] != previous_api_pid:
                base = wait_ready(data, process)
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("Packaged API did not recover after process loss")
        print(
            "PASS: packaged API recovered after a real process termination", flush=True
        )
        stop(process)
        process = launch()
        base = wait_ready(data, process)
        config = json.loads((data / "config.json").read_text())
        evidence = json.loads((data / "verification-evidence.json").read_text())
        with httpx.Client(base_url=base, timeout=15, trust_env=False) as client:
            nonce = client.post(
                "/api/v1/local/bootstrap", json={"launcher_key": config["launcher_key"]}
            )
            nonce.raise_for_status()
            login = client.post("/api/v1/local/login", json=nonce.json())
            login.raise_for_status()
            client.headers["Authorization"] = "Bearer " + login.json()["access_token"]
            clip = client.get("/api/v1/clips/" + evidence["clip_id"])
            clip.raise_for_status()
            assert clip.json()["is_reviewed"] is True
            assert (
                clip.json()["label_data"]["offensive_formation"]["formation"]
                == "Verification tag"
            )
            assert client.get("/api/v1/auth/me").status_code == 200
            assert client.get("/").status_code == 200
        print(
            "PASS: packaged PostgreSQL library, annotations, review and UI survive restart",
            flush=True,
        )
    finally:
        stop(process)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
