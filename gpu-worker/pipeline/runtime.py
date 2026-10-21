"""Cooperative job control shared by stages and the local worker."""

from __future__ import annotations

import contextvars
import hashlib
import os
from collections.abc import Callable
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any


class PipelineInterrupted(RuntimeError):
    """Stop at a safe boundary while keeping accepted stage checkpoints."""

    def __init__(self, action: str) -> None:
        self.action = action
        super().__init__(f"Processing interrupted: {action}")


_control: contextvars.ContextVar[Callable[[], str | None] | None] = contextvars.ContextVar(
    "pipeline_control", default=None
)
_lease: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "pipeline_lease", default=None
)


@contextmanager
def job_context(
    *,
    control: Callable[[], str | None] | None = None,
    lease: dict[str, Any] | None = None,
):
    control_token = _control.set(control)
    lease_token = _lease.set(dict(lease or {}))
    try:
        yield
    finally:
        _lease.reset(lease_token)
        _control.reset(control_token)


def _raise_on_control() -> None:
    callback = _control.get()
    action = callback() if callback is not None else None
    if action:
        raise PipelineInterrupted(action)


def check_control() -> None:
    """Honour cancel/pause, and pause while the GPU is too hot (see thermal)."""
    _raise_on_control()
    if _control.get() is not None:  # only inside a job, never in tests' bare calls
        from pipeline.thermal import governor

        governor.pace(_raise_on_control)


def lease_headers() -> dict[str, str]:
    lease = _lease.get() or {}
    if not lease.get("lease_token"):
        return {}
    return {
        "X-Job-ID": str(lease["job_id"]),
        "X-Worker-ID": str(lease["worker_id"]),
        "X-Job-Lease-Token": str(lease["lease_token"]),
    }


def lease_payload(job_id: str) -> dict[str, Any]:
    lease = _lease.get() or {}
    if str(lease.get("job_id")) != str(job_id):
        return {}
    return {
        key: lease[key]
        for key in ("worker_id", "lease_token", "attempt_count")
        if lease.get(key) is not None
    }


def set_checkpoint_identity(fingerprint: str, source_fingerprint: str | None = None) -> None:
    lease = dict(_lease.get() or {})
    lease["checkpoint_fingerprint"] = fingerprint
    lease["source_fingerprint"] = source_fingerprint or fingerprint
    _lease.set(lease)


def stable_record_id(path: str, payload: dict[str, Any]) -> str | None:
    """Make half-published stages replay safely after interruption/reclaim."""
    import json
    import uuid

    lease = _lease.get() or {}
    fingerprint = lease.get("checkpoint_fingerprint")
    if not fingerprint:
        return None
    values = {key: value for key, value in payload.items() if key not in {"job_id", "id"}}
    is_clip = path.endswith("/clips") or (
        path == "offline" and "video_id" in values and "start_time" in values
    )
    if is_clip:
        fingerprint = lease.get("source_fingerprint", fingerprint)
        values = {
            key: value
            for key, value in values.items()
            if key in {"video_id", "start_time", "end_time", "play_number"}
        }
    key = json.dumps([fingerprint, path, values], sort_keys=True, default=str)
    return str(uuid.uuid5(uuid.NAMESPACE_URL, key))


class ModelUnavailableError(RuntimeError):
    """A requested real model cannot run; callers must not fabricate outputs."""


def inference_device() -> str:
    """Select CPU by default; accelerator requests must be available explicitly."""
    requested = os.environ.get("PIPELINE_DEVICE", "cpu").strip().lower()
    if requested not in {"cpu", "auto", "cuda", "cuda:0", "mps"}:
        raise ValueError(f"Unsupported PIPELINE_DEVICE: {requested}")
    if requested == "cpu":
        return "cpu"
    try:
        import torch
    except ImportError as exc:
        if requested == "auto":
            return "cpu"
        raise ModelUnavailableError("PyTorch is required for acceleration.") from exc
    if requested == "auto":
        # MPS is excluded until the model operations have been validated on Mac.
        return "cuda:0" if torch.cuda.is_available() else "cpu"
    if requested.startswith("cuda") and torch.cuda.is_available():
        return "cuda:0"
    mps = getattr(getattr(torch, "backends", None), "mps", None)
    if requested == "mps" and mps is not None and mps.is_available():
        return "mps"
    raise ModelUnavailableError(f"Requested inference device {requested} is unavailable. Use cpu.")


def configure_cpu_threads() -> int:
    """Bound inference threads, leaving capacity for playback and the API."""
    available = os.cpu_count() or 1
    default = max(1, min(4, available // 2))
    threads = max(1, min(available, int(os.environ.get("PIPELINE_CPU_THREADS", default))))
    try:
        import torch

        torch.set_num_threads(threads)
    except ImportError:
        pass
    return threads


@lru_cache(maxsize=32)
def _file_hash(path: str, size: int, modified_ns: int) -> str:
    del size, modified_ns
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def model_provenance(
    *, effective_variant: str, source: str, device: str = "cpu", model_path: str | None = None
) -> dict[str, Any]:
    fingerprint = None
    if model_path:
        path = Path(model_path).expanduser()
        if path.is_file():
            stat = path.stat()
            fingerprint = _file_hash(str(path.resolve()), stat.st_size, stat.st_mtime_ns)
    return {
        "effective_variant": effective_variant,
        "source": source,
        "device": device,
        "model_sha256": fingerprint,
    }


def adapter_provenance(adapter: Any, requested_variant: str) -> dict[str, Any]:
    """Unwrap composition so routing requests do not impersonate actual execution."""
    current = adapter
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        provenance = getattr(current, "provenance", None)
        if provenance is not None:
            return {"requested_variant": requested_variant, **provenance}
        current = getattr(current, "_inner", None) or getattr(current, "inner", None)
    return {
        "requested_variant": requested_variant,
        "effective_variant": "disabled" if adapter is None else type(adapter).__name__,
        "source": "disabled" if adapter is None else "unverified_adapter",
        "device": None,
        "model_sha256": None,
    }
