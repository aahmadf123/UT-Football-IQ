"""Offline upload queue — clips auto-queue when drone connectivity restores.

Drone uploads land in a local SQLite-backed queue when the tablet/drone has no
connection to the backend. When connectivity returns the queue is flushed: each
pending entry is dispatched via ``queue.job_dispatch``, which inserts a
claimable ``processing_jobs`` row.

Design:
  - Storage: SQLite file at OFFLINE_QUEUE_DB_PATH (default: /tmp/offline_queue.db)
  - Connectivity check: HEAD request to CONNECTIVITY_CHECK_URL, which defaults
    to the backend's own /health endpoint — reaching the backend is what
    actually matters, not general internet access.
  - Flush loop: runs in a background thread, polls every FLUSH_POLL_INTERVAL s
  - Thread-safe: all DB access is serialised with a threading.Lock

Environment variables:
  OFFLINE_QUEUE_DB_PATH  — path to SQLite DB (default: /tmp/offline_queue.db)
  CONNECTIVITY_CHECK_URL — URL used to probe reachability (default:
                           ``{BACKEND_API_URL}/health``)
  FLUSH_POLL_INTERVAL    — seconds between flush attempts (default: 15)
  BACKEND_API_URL        — forwarded to job_dispatch
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from queue.job_dispatch import dispatch_on_upload
from typing import Any

import httpx
import structlog

log = structlog.get_logger(__name__)

OFFLINE_QUEUE_DB_PATH = os.environ.get("OFFLINE_QUEUE_DB_PATH", "/tmp/offline_queue.db")
FLUSH_POLL_INTERVAL = int(os.environ.get("FLUSH_POLL_INTERVAL", "15"))


def _connectivity_check_url() -> str:
    """Probe target for :func:`is_online`.

    Resolved lazily so a late ``BACKEND_API_URL`` still takes effect. Returns
    an empty string when there is no backend configured, which makes
    :func:`is_online` report offline rather than probing an unrelated host.
    """
    explicit = os.environ.get("CONNECTIVITY_CHECK_URL", "").strip()
    if explicit:
        return explicit
    backend = os.environ.get("BACKEND_API_URL", "").strip()
    return f"{backend.rstrip('/')}/health" if backend else ""

_db_lock = threading.Lock()
_flush_thread: threading.Thread | None = None
_stop_event = threading.Event()


# ── Database helpers ──────────────────────────────────────────────────────────


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(OFFLINE_QUEUE_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS offline_queue (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id        TEXT    NOT NULL,
            storage_uri     TEXT    NOT NULL,
            job_type        TEXT    NOT NULL DEFAULT 'ingest',
            is_same_session INTEGER NOT NULL DEFAULT 0,
            clip_id         TEXT,
            input_artifacts TEXT,
            enqueued_at     REAL    NOT NULL,
            attempts        INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.commit()


def _init_db() -> None:
    with _db_lock:
        conn = _get_conn()
        try:
            _ensure_schema(conn)
        finally:
            conn.close()


# ── Public API ────────────────────────────────────────────────────────────────


def enqueue(
    video_id: str,
    storage_uri: str,
    job_type: str = "ingest",
    *,
    is_same_session: bool = False,
    clip_id: str | None = None,
    input_artifacts: dict[str, Any] | None = None,
) -> int:
    """Add a pending upload to the offline queue.

    Returns the row ID of the inserted entry.
    """
    _init_db()
    artifacts_json = json.dumps(input_artifacts) if input_artifacts else None

    with _db_lock:
        conn = _get_conn()
        try:
            cur = conn.execute(
                """
                INSERT INTO offline_queue
                    (video_id, storage_uri, job_type, is_same_session,
                     clip_id, input_artifacts, enqueued_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    video_id,
                    storage_uri,
                    job_type,
                    int(is_same_session),
                    clip_id,
                    artifacts_json,
                    time.time(),
                ),
            )
            conn.commit()
            row_id: int = cur.lastrowid or 0
            log.info(
                "offline_queue_enqueued",
                row_id=row_id,
                video_id=video_id,
                is_same_session=is_same_session,
            )
            return row_id
        finally:
            conn.close()


def pending_count() -> int:
    """Return the number of entries waiting to be flushed."""
    _init_db()
    with _db_lock:
        conn = _get_conn()
        try:
            row = conn.execute("SELECT COUNT(*) FROM offline_queue").fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()


def flush_once(*, client: httpx.Client | None = None) -> int:
    """Try to dispatch all pending entries now.

    Returns the number of successfully dispatched entries.
    """
    _init_db()
    dispatched = 0

    with _db_lock:
        conn = _get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM offline_queue ORDER BY id ASC"
            ).fetchall()
        finally:
            conn.close()

    for row in rows:
        row_id: int = row["id"]
        video_id: str = row["video_id"]
        storage_uri: str = row["storage_uri"]
        job_type: str = row["job_type"]
        is_same_session: bool = bool(row["is_same_session"])
        clip_id: str | None = row["clip_id"]
        artifacts: dict[str, Any] | None = (
            json.loads(row["input_artifacts"]) if row["input_artifacts"] else None
        )

        try:
            dispatch_on_upload(
                video_id=video_id,
                storage_uri=storage_uri,
                job_type=job_type,
                is_same_session=is_same_session,
                clip_id=clip_id,
                input_artifacts=artifacts,
                client=client,
            )
            _delete_row(row_id)
            dispatched += 1
            log.info("offline_queue_flushed", row_id=row_id, video_id=video_id)
        except Exception as exc:
            _increment_attempts(row_id)
            log.warning("offline_queue_flush_error", row_id=row_id, error=str(exc))

    return dispatched


def _delete_row(row_id: int) -> None:
    with _db_lock:
        conn = _get_conn()
        try:
            conn.execute("DELETE FROM offline_queue WHERE id = ?", (row_id,))
            conn.commit()
        finally:
            conn.close()


def _increment_attempts(row_id: int) -> None:
    with _db_lock:
        conn = _get_conn()
        try:
            conn.execute(
                "UPDATE offline_queue SET attempts = attempts + 1 WHERE id = ?",
                (row_id,),
            )
            conn.commit()
        finally:
            conn.close()


# ── Connectivity check ────────────────────────────────────────────────────────


def is_online(*, timeout: float = 5.0) -> bool:
    """Return True when the backend is reachable from this device."""
    url = _connectivity_check_url()
    if not url:
        return False
    try:
        with httpx.Client(timeout=timeout) as c:
            resp = c.head(url)
            return resp.status_code < 500
    except Exception:
        return False


# ── Background flush loop ─────────────────────────────────────────────────────


def start_flush_loop() -> threading.Thread:
    """Start the background connectivity-watch / flush thread.

    Safe to call multiple times — subsequent calls are no-ops if the thread
    is already running.
    """
    global _flush_thread
    if _flush_thread is not None and _flush_thread.is_alive():
        return _flush_thread

    _stop_event.clear()
    _flush_thread = threading.Thread(target=_flush_loop, daemon=True, name="offline-flush")
    _flush_thread.start()
    log.info("offline_flush_loop_started", poll_interval=FLUSH_POLL_INTERVAL)
    return _flush_thread


def stop_flush_loop() -> None:
    """Signal the background flush loop to stop."""
    _stop_event.set()


def _flush_loop() -> None:
    _init_db()
    while not _stop_event.is_set():
        if pending_count() > 0 and is_online():
            try:
                n = flush_once()
                if n:
                    log.info("offline_queue_flush_complete", dispatched=n)
            except Exception as exc:
                log.error("offline_flush_loop_error", error=str(exc))
        _stop_event.wait(timeout=float(FLUSH_POLL_INTERVAL))
    log.info("offline_flush_loop_stopped")
