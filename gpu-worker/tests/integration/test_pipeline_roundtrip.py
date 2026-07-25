"""Cross-service round trip: API register → pipeline job → worker → clips.

Boots the real backend (uvicorn subprocess) against the configured Postgres,
seeds users, uploads a synthetic clip THROUGH THE API (the Worker-parity
endpoints), lets auto-process enqueue the pipeline job, runs one worker
claim/process cycle in-process, and asserts the results landed via the API:
job succeeded with per-stage progress, clips exist, tracklets exist, and the
overlay payload serves.

Run explicitly (excluded from the unit run):

    pytest -m integration tests/integration/

Requires: DATABASE_URL (Postgres with pgvector), ffmpeg. Detection and
segmentation are pinned to their deterministic stub variants: this test
verifies cross-service wiring, and real models legitimately find nothing in
a synthetic shapes clip (real-footage acceptance covers model quality).
"""

from __future__ import annotations

import importlib.util
import os
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.integration

# Route detection to the deterministic StubDetector: real YOLO (correctly)
# finds no people in a synthetic shapes clip, and this test asserts the
# tracklet writeback chain, not model quality.
_STUB_ROUTING = '{"detect": {"same_session": "stub", "nightly": "stub"}}'

_ROOT = Path(__file__).resolve().parents[2]
_REPO = _ROOT.parent
_BACKEND_DIR = _REPO / "backend"

cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")

_ADMIN = {"email": "it-admin@example.com", "password": "it-admin-pass-123"}
_WORKER = {"email": "it-worker@example.com", "password": "it-worker-pass-123"}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _backend_python() -> str:
    venv = _BACKEND_DIR / ".venv" / "bin" / "python"
    return str(venv) if venv.exists() else sys.executable


@pytest.fixture(scope="module")
def backend(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        pytest.skip("DATABASE_URL not configured")

    storage_root = tmp_path_factory.mktemp("it-storage")
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    env = {
        **os.environ,
        "DATABASE_URL": database_url,
        "SECRET_KEY": "integration-secret",
        "ENVIRONMENT": "test",
        "STORAGE_BACKEND": "local",
        "LOCAL_STORAGE_ROOT": str(storage_root),
        "PUBLIC_API_BASE_URL": base,
        "SCHEDULER_ENABLED": "0",
    }
    python = _backend_python()

    subprocess.run(
        [python, "-m", "alembic", "upgrade", "head"],
        cwd=_BACKEND_DIR,
        env=env,
        check=True,
        capture_output=True,
    )
    seed_env = {
        **env,
        "SEED_ADMIN_EMAIL": _ADMIN["email"],
        "SEED_ADMIN_PASSWORD": _ADMIN["password"],
        "SEED_WORKER_EMAIL": _WORKER["email"],
        "SEED_WORKER_PASSWORD": _WORKER["password"],
    }
    subprocess.run(
        [python, "-m", "scripts.seed_users"],
        cwd=_BACKEND_DIR,
        env=seed_env,
        check=True,
        capture_output=True,
    )

    # Log to a file, never a PIPE: nobody drains the pipe during the test, and
    # once the writeback + access-log volume fills the 64 KB buffer, uvicorn's
    # event loop blocks mid-write and every request stalls (observed as the
    # worker grinding through serial 30 s writeback timeouts).
    backend_log = storage_root.parent / "backend-uvicorn.log"
    log_handle = backend_log.open("wb")
    proc = subprocess.Popen(
        [python, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=_BACKEND_DIR,
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                if httpx.get(f"{base}/health", timeout=2).status_code == 200:
                    break
            except Exception:
                time.sleep(0.5)
        else:
            proc.terminate()
            raise RuntimeError("backend did not become healthy")

        # Point this process's worker-side modules at the live backend.
        os.environ["BACKEND_API_URL"] = base
        os.environ["WORKER_EMAIL"] = _WORKER["email"]
        os.environ["WORKER_PASSWORD"] = _WORKER["password"]
        os.environ["STORAGE_BACKEND"] = "local"
        os.environ["LOCAL_STORAGE_ROOT"] = str(storage_root)
        # Deterministic segmentation + detection: this test verifies
        # cross-service wiring, not model quality (the optical-flow segmenter
        # has no stable behaviour on tiny synthetic clips, and real YOLO finds
        # no people in one — real-footage acceptance covers both).
        os.environ["SEGMENTER_VARIANT"] = "stub"
        routing = storage_root / "stub-routing.json"
        routing.write_text(_STUB_ROUTING)
        os.environ["MODEL_ROUTING_CONFIG"] = str(routing)
        yield base
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        log_handle.close()


def _synthetic_video(path: Path) -> None:
    # 5 s of SMOOTH ping-pong motion — comfortably above stage_segment's
    # MIN_PLAY_DURATION (3 s), with no teleport discontinuity (a wrap-around
    # jump reads as a play boundary to the optical-flow segmenter and can
    # split the clip into sub-minimum segments).
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 30.0, (320, 240))
    assert writer.isOpened()
    for i in range(150):
        frame = np.zeros((240, 320, 3), dtype=np.uint8)
        pos = (i * 3) % 540
        x = 10 + (pos if pos < 270 else 540 - pos)
        frame[100:140, x : x + 30] = (0, 255, 0)
        writer.write(frame)
    writer.release()


def _load_worker_main():  # noqa: ANN202 — module type
    spec = importlib.util.spec_from_file_location("gpu_worker_main_it", _ROOT / "__main__.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("gpu_worker_main_it", module)
    spec.loader.exec_module(module)
    return module


def _poll(fetch, accept, *, timeout: float = 30.0, interval: float = 0.25):  # noqa: ANN001,ANN202
    """Poll an eventually-consistent read until ``accept`` is satisfied.

    The backend commits in the ``get_db`` dependency's teardown — i.e. after
    the response is sent — so an immediate read-after-write can legitimately
    observe stale data (a ``POST /videos`` can return 201 before its own row
    is visible; a worker's ``PATCH … succeeded`` returns 200 before the commit
    lands). The real frontend polls for exactly this reason; the round trip
    must too, or it races the commit and flakes on fast machines. Returns the
    last value so the caller's assertion can still produce a useful message.
    """
    deadline = time.monotonic() + timeout
    value = fetch()
    while not accept(value) and time.monotonic() < deadline:
        time.sleep(interval)
        value = fetch()
    return value


def test_full_round_trip(backend: str, tmp_path: Path) -> None:
    client = httpx.Client(base_url=backend, timeout=120)

    login = client.post("/api/v1/auth/login", json=_ADMIN)
    login.raise_for_status()
    auth = {"Authorization": f"Bearer {login.json()['access_token']}"}

    # Upload through the Worker-parity backend endpoints.
    source = tmp_path / "synthetic.mp4"
    _synthetic_video(source)
    up = client.post("/api/v1/videos/upload-url", headers=auth, json={"filename": source.name})
    up.raise_for_status()
    put = client.put(
        up.json()["uploadUrl"],
        headers={**auth, "Content-Type": "video/mp4"},
        content=source.read_bytes(),
    )
    put.raise_for_status()

    register = client.post(
        "/api/v1/videos",
        headers=auth,
        json={
            "filename": source.name,
            "storage_uri": put.json()["storageUri"],
            "session_kind": "practice",
            "our_possession": "offense",
        },
    )
    register.raise_for_status()
    video_id = register.json()["id"]

    # auto_process_on_upload (default ON) must have enqueued the pipeline job
    # (the enqueue commits in teardown, so poll rather than read once).
    jobs = _poll(
        lambda: client.get(f"/api/v1/videos/{video_id}/jobs", headers=auth).json(),
        lambda j: isinstance(j, list) and bool(j),
    )
    assert jobs and jobs[0]["job_type"] == "pipeline", jobs
    job_id = jobs[0]["id"]

    # One worker cycle in-process: claim the job and run it.
    worker_main = _load_worker_main()
    with httpx.Client() as worker_client:
        claimed = worker_main.claim_db_job(worker_client)
    assert claimed is not None and str(claimed["id"]) == str(job_id)
    worker_main.process_job(worker_main._db_row_to_job(claimed))

    # The worker's terminal PATCH returns 200 before its commit is visible, so
    # poll for a terminal state instead of reading once.
    job = _poll(
        lambda: client.get(f"/api/v1/jobs/{job_id}", headers=auth).json(),
        lambda j: j.get("status") in ("succeeded", "failed"),
    )
    assert job["status"] == "succeeded", job.get("error_message")
    assert job["progress"], "per-stage progress missing"
    assert any(k.startswith("detect") for k in job["progress"])
    assert (job["output_artifacts"] or {}).get("clip_count", 0) >= 1

    clips = _poll(
        lambda: client.get(f"/api/v1/videos/{video_id}/clips", headers=auth).json(),
        lambda c: isinstance(c, list) and bool(c),
    )
    assert clips, "no clips created"

    overlays = client.get(f"/api/v1/clips/{clips[0]['id']}/overlays", headers=auth).json()
    assert "calibration" in overlays
    assert overlays["layers_available"]["tracklets"] in (True, False)

    def _tracklet_total() -> int:
        return sum(
            len(client.get(f"/api/v1/clips/{c['id']}/overlays", headers=auth).json()["tracklets"])
            for c in clips
        )

    tracklet_total = _poll(_tracklet_total, lambda n: n > 0)
    assert tracklet_total > 0, "no tracklets persisted"

    _ = uuid.UUID(video_id)  # ids are real UUIDs end-to-end
