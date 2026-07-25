"""Pipeline orchestrator — walks a video through every processing stage.

This is the missing spine between "a raw video file / storage URI" and
"clips with detections, tracklets, events, labels, metrics, and rendered
overlays". Historically each stage ran as its own queue message and nothing
chained them; artifacts were expected to ride inside queue payloads (which
breaks Cloudflare's 128 KB message cap on real footage). Here the stages run
**in-process**: artifacts pass by reference in memory, and JSON snapshots of
each stage's outputs are written through an :class:`ArtifactSink` as a
resume ledger and a debugging record.

Two callers:

* ``gpu-worker/__main__.py`` — a claimed ``pipeline`` job runs
  :func:`run_pipeline` with a ``progress_cb`` that heartbeats per-stage
  progress into the backend job row.
* ``python -m pipeline run`` (:mod:`pipeline.__main__`) — the turnkey local
  CLI: any video file in, clips/boxes/overlays out, no backend or cloud
  credentials required.

Stage order comes from :mod:`pipeline.lightweight_config`
(``SAME_SESSION_STAGES`` / ``NIGHTLY_STAGES``) — this module finally makes
those lists live. Video-scoped stages (ingest → segment → calibrate →
detect) run once; clip-scoped stages run per clip proposed by segment.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)

#: Stages that operate on the whole video, in canonical order.
VIDEO_STAGES: tuple[str, ...] = ("ingest", "segment", "calibrate", "detect")

#: Stages that operate per clip, in canonical order.
CLIP_STAGES: tuple[str, ...] = (
    "track",
    "reid",
    "pose",
    "events",
    "labels",
    "metrics",
    "routes",
    "coverage",
    "oline",
    "pressure",
    "render",
    "render_hls",
)

#: Stages that only make sense against a live backend (they read aggregate
#: data back out of the API rather than transforming this video's artifacts).
BACKEND_COUPLED_STAGES: frozenset[str] = frozenset(
    {"self_scout", "embeddings", "workload_rollup"}
)

ProgressCallback = Callable[[str, str | None, str, dict[str, Any]], None]
"""(stage, clip_id | None, status: started|succeeded|failed|skipped, extra)"""


def _json_default(obj: Any) -> Any:
    """Best-effort JSON coercion for numpy scalars/arrays in artifacts."""
    try:
        import numpy as np

        if isinstance(obj, np.integer):
            return int(obj)
        if isinstance(obj, np.floating):
            return float(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
    except ImportError:
        pass
    return str(obj)


# ── Artifact sinks (resume ledger + debug record) ─────────────────────────────


class ArtifactSink:
    """Writes per-stage JSON snapshots; ``load`` powers resume."""

    def write(self, stage: str, payload: dict[str, Any], clip_id: str | None = None) -> str | None:
        raise NotImplementedError

    def load(self, stage: str, clip_id: str | None = None) -> dict[str, Any] | None:
        raise NotImplementedError


class NullArtifactSink(ArtifactSink):
    """No persistence — artifacts live only in memory for this run."""

    def write(self, stage: str, payload: dict[str, Any], clip_id: str | None = None) -> str | None:
        return None

    def load(self, stage: str, clip_id: str | None = None) -> dict[str, Any] | None:
        return None


class DirArtifactSink(ArtifactSink):
    """Writes ``{root}/{stage}[.{clip_id}].json`` on the local filesystem."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, stage: str, clip_id: str | None) -> Path:
        name = f"{stage}.{clip_id}.json" if clip_id else f"{stage}.json"
        return self.root / name

    def write(self, stage: str, payload: dict[str, Any], clip_id: str | None = None) -> str | None:
        path = self._path(stage, clip_id)
        path.write_text(json.dumps(payload, default=_json_default))
        return str(path)

    def load(self, stage: str, clip_id: str | None = None) -> dict[str, Any] | None:
        path = self._path(stage, clip_id)
        if not path.is_file():
            return None
        try:
            data: dict[str, Any] = json.loads(path.read_text())
            return data
        except (OSError, json.JSONDecodeError):
            return None


class StorageArtifactSink(ArtifactSink):
    """Writes ledger JSON through the storage facade (r2:// or local://)."""

    def __init__(self, video_id: str) -> None:
        self.video_id = video_id
        self._refs: dict[str, str] = {}

    def _key(self, stage: str, clip_id: str | None) -> str:
        name = f"{stage}.{clip_id}.json" if clip_id else f"{stage}.json"
        return f"pipeline_artifacts/{self.video_id}/{name}"

    def write(self, stage: str, payload: dict[str, Any], clip_id: str | None = None) -> str | None:
        from pipeline import storage

        try:
            data = json.dumps(payload, default=_json_default).encode()
            uri = storage.upload_bytes(
                data, self._key(stage, clip_id), content_type="application/json"
            )
            self._refs[self._key(stage, clip_id)] = uri
            return uri
        except Exception as exc:
            log.warning("artifact_ledger_write_failed", stage=stage, error=str(exc))
            return None

    def load(self, stage: str, clip_id: str | None = None) -> dict[str, Any] | None:
        from pipeline import storage

        ref = self._refs.get(self._key(stage, clip_id))
        if not ref:
            return None
        try:
            path = storage.download_to_temp(ref)
        except Exception:
            return None
        try:
            data: dict[str, Any] = json.loads(path.read_text())
            return data
        except (OSError, json.JSONDecodeError):
            return None
        finally:
            path.unlink(missing_ok=True)


# ── Context ───────────────────────────────────────────────────────────────────


@dataclass
class PipelineContext:
    """Mutable state threaded through the stage chain for one video."""

    video_id: str
    input_uri: str
    priority: int = 0
    job_id: str = "local"
    roster: list[dict[str, Any]] = field(default_factory=list)

    # Populated by video-scoped stages
    video_path: Path | None = None  # master local copy, downloaded once
    fps: float = 30.0
    capture_regime: str | None = None
    regime_confidence: float = 0.0
    analytics_safe: bool = False
    calibration: dict[str, Any] = field(default_factory=dict)
    detections: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    clips: list[dict[str, Any]] = field(default_factory=list)

    # Per-stage outputs: stage → artifacts (video-scoped) and
    # clip_id → stage → artifacts (clip-scoped)
    stage_artifacts: dict[str, dict[str, Any]] = field(default_factory=dict)
    clip_artifacts: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    model_routing: dict[str, str] = field(default_factory=dict)


# ── Per-clip helpers ──────────────────────────────────────────────────────────


def _clip_frame_window(clip: dict[str, Any], fps: float) -> tuple[int, int]:
    start_t = float(clip.get("start_time") or 0.0)
    end_t = float(clip.get("end_time") or 0.0)
    return int(start_t * fps), max(int(end_t * fps), int(start_t * fps))


def _slice_detections(
    detections: dict[str, list[dict[str, Any]]], start_frame: int, end_frame: int
) -> dict[str, list[dict[str, Any]]]:
    return {
        f: dets for f, dets in detections.items() if start_frame <= int(f) <= end_frame
    }


def _offense_direction(labels: list[dict[str, Any]]) -> int:
    """Map the play_direction label to the +1/-1 convention downstream."""
    for lbl in labels:
        if lbl.get("label_type") == "play_direction":
            direction = str(lbl.get("label_value", {}).get("direction", "")).lower()
            if direction in ("left", "-1"):
                return -1
            if direction in ("right", "1", "+1"):
                return 1
    return 1


# ── Stage runners ─────────────────────────────────────────────────────────────


def _run_video_stage(stage: str, ctx: PipelineContext) -> dict[str, Any]:
    """Run one video-scoped stage and fold its outputs into the context."""
    from pipeline import model_router

    # Stages re-materialise the video from this reference themselves; the
    # master temp copy keeps that a local file copy, never a re-download.
    local_uri = f"file://{ctx.video_path}" if ctx.video_path else ctx.input_uri

    if stage == "ingest":
        from pipeline import stage_ingest

        artifacts = stage_ingest.run(ctx.video_id, local_uri, ctx.job_id)
        ctx.capture_regime = artifacts.get("capture_regime") or ctx.capture_regime
        ctx.regime_confidence = float(artifacts.get("regime_confidence") or 0.0)
        probe = artifacts.get("probe") or {}
        if probe.get("fps"):
            ctx.fps = float(probe["fps"])
        return artifacts

    if stage == "segment":
        from pipeline import stage_segment

        artifacts = stage_segment.run(
            ctx.video_id, local_uri, ctx.job_id, priority=ctx.priority
        )
        ctx.clips = [c for c in artifacts.get("clips", []) if c.get("id")]
        return artifacts

    if stage == "calibrate":
        from pipeline import stage_calibrate

        variant = model_router.select_model("calibrate", ctx.priority)
        artifacts = stage_calibrate.run(
            ctx.video_id,
            local_uri,
            ctx.job_id,
            variant=variant,
            capture_regime=ctx.capture_regime,
        )
        ctx.analytics_safe = bool(artifacts.get("analytics_safe", False))
        ctx.calibration = artifacts
        return artifacts

    if stage == "detect":
        from pipeline import stage_detect

        variant = model_router.select_detect_variant(ctx.priority, ctx.capture_regime)
        artifacts = stage_detect.run(
            ctx.video_id,
            local_uri,
            ctx.job_id,
            variant=variant,
            capture_regime=ctx.capture_regime,
            priority=ctx.priority,
        )
        ctx.detections = artifacts.get("detections", {})
        if artifacts.get("fps"):
            ctx.fps = float(artifacts["fps"])
        return artifacts

    raise ValueError(f"Unknown video-scoped stage: {stage}")


def _run_clip_stage(
    stage: str, ctx: PipelineContext, clip: dict[str, Any]
) -> dict[str, Any]:
    """Run one clip-scoped stage for ``clip`` and fold outputs into context."""
    from pipeline import model_router

    clip_id = str(clip["id"])
    per_clip = ctx.clip_artifacts.setdefault(clip_id, {})
    tracklets: list[dict[str, Any]] = per_clip.get("track", {}).get("tracklets", [])
    events_list: list[dict[str, Any]] = per_clip.get("events", {}).get("events", [])
    labels_list: list[dict[str, Any]] = per_clip.get("labels", {}).get("labels", [])
    metrics_list: list[dict[str, Any]] = per_clip.get("metrics", {}).get("metrics", [])
    pose_metrics: dict[str, Any] = per_clip.get("pose", {}).get("pose_metrics", {})
    start_frame, end_frame = _clip_frame_window(clip, ctx.fps)

    if stage == "track":
        from pipeline import stage_track

        variant = model_router.select_model("track", ctx.priority)
        clip_dets = _slice_detections(ctx.detections, start_frame, end_frame)
        return stage_track.run(clip_id, clip_dets, ctx.fps, ctx.job_id, variant=variant)

    if stage == "reid":
        from pipeline import stage_reid
        from pipeline.backend import BACKEND_API_URL

        if ctx.video_path is None:
            return {"reid_skipped": True, "reason": "no_video_path"}
        variant = model_router.select_model("reid", ctx.priority)
        return stage_reid.run(
            clip_id,
            ctx.video_path,
            [t["id"] for t in tracklets],
            tracklets,
            ctx.roster,
            BACKEND_API_URL,
            variant=variant,
            priority=ctx.priority,
        )

    if stage == "pose":
        import os

        from pipeline import stage_pose, video_ingest

        model_path = os.environ.get("MODEL_POSE_PATH")
        source_ref = str(ctx.video_path) if ctx.video_path else None
        with video_ingest.open_video(source_ref, fps_fallback=ctx.fps) as source:
            return stage_pose.run(
                clip_id,
                source,
                tracklets,
                events_list,
                ctx.analytics_safe,
                ctx.fps,
                ctx.job_id,
                model_path,
            )

    if stage == "events":
        from pipeline import stage_events

        clip_dets = _slice_detections(ctx.detections, start_frame, end_frame)
        return stage_events.run(
            clip_id,
            clip_dets,
            ctx.fps,
            ctx.job_id,
            tracklets=tracklets or None,
        )

    if stage == "labels":
        from pipeline import stage_labels

        local_uri = f"file://{ctx.video_path}" if ctx.video_path else ctx.input_uri
        return stage_labels.run(clip_id, tracklets, events_list, ctx.fps, local_uri)

    if stage == "metrics":
        from pipeline import stage_metrics

        return stage_metrics.run(
            clip_id,
            tracklets,
            events_list,
            ctx.analytics_safe,
            ctx.fps,
            ctx.job_id,
            pose_metrics=pose_metrics or None,
        )

    if stage == "routes":
        from pipeline import stage_routes

        return stage_routes.run(clip_id, tracklets, events_list, ctx.fps)

    if stage == "coverage":
        from pipeline import stage_coverage

        return stage_coverage.run(
            clip_id,
            tracklets,
            events_list,
            ctx.fps,
            analytics_safe=ctx.analytics_safe,
            offense_direction=_offense_direction(labels_list),
        )

    if stage == "oline":
        from pipeline import stage_oline

        return stage_oline.run(
            clip_id, tracklets, events_list, ctx.analytics_safe, ctx.fps, ctx.job_id
        )

    if stage == "pressure":
        from pipeline import stage_pressure

        return stage_pressure.run(
            clip_id,
            tracklets,
            events_list,
            ctx.analytics_safe,
            ctx.fps,
            ctx.job_id,
            offense_direction=_offense_direction(labels_list),
            down=None,
            distance_yards=None,
        )

    if stage == "render":
        from pipeline import stage_render
        from pipeline.backend import BACKEND_API_URL
        from pipeline.lightweight_config import use_period_renderer
        from renderer import period_renderer

        if ctx.video_path is None:
            return {"render_skipped": True, "reason": "no_video_path"}
        if use_period_renderer(ctx.priority):
            return period_renderer.run(
                clip_id,
                ctx.video_path,
                tracklets,
                labels_list,
                ctx.analytics_safe,
                ctx.fps,
            )
        return stage_render.run(
            clip_id,
            ctx.video_path,
            tracklets,
            labels_list,
            metrics_list,
            ctx.analytics_safe,
            ctx.fps,
            BACKEND_API_URL,
        )

    if stage == "render_hls":
        from pipeline import storage
        from renderer import hls_encoder

        render_artifacts = per_clip.get("render", {})
        overlay_uri = render_artifacts.get("overlay_uri") or render_artifacts.get(
            "period_overlay_uri", ""
        )
        if not overlay_uri:
            return {"hls_skipped": True, "reason": "no_overlay_uri"}
        overlay_path = storage.download_to_temp(overlay_uri)
        try:
            return hls_encoder.run(clip_id, overlay_path, ctx.fps)
        finally:
            overlay_path.unlink(missing_ok=True)

    raise ValueError(f"Unknown clip-scoped stage: {stage}")


# ── Public entrypoint ─────────────────────────────────────────────────────────


def run_pipeline(
    video_id: str,
    input_uri: str,
    *,
    priority: int = 0,
    job_id: str = "local",
    stages: list[str] | None = None,
    progress_cb: ProgressCallback | None = None,
    artifact_sink: ArtifactSink | None = None,
    roster: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run the full processing chain for one video and return a summary.

    Args:
        video_id: Backend video id (or any identifier for local runs).
        input_uri: ``r2://`` / ``local://`` / ``file://`` reference or plain
            path to the source video.
        priority: Routing priority — ≥ 10 selects the same-session
            (fast/preliminary) path, otherwise nightly (full quality).
        job_id: Backend job id when driven by the worker; ``"local"`` for CLI.
        stages: Explicit stage list; defaults to the mode's configured list.
        progress_cb: Per-stage progress hook (worker → job heartbeat).
        artifact_sink: Ledger for stage-output JSON; completed stages found
            in the ledger are skipped (resume support).
        roster: Optional roster records for jersey re-ID.
    """
    from pipeline import model_router, storage
    from pipeline.lightweight_config import (
        NIGHTLY_STAGES,
        SAME_SESSION_STAGES,
        should_skip_stage,
    )

    started = time.monotonic()
    sink = artifact_sink or NullArtifactSink()
    is_same_session = model_router.is_same_session(priority)
    stage_list = list(
        stages
        if stages is not None
        else (SAME_SESSION_STAGES if is_same_session else NIGHTLY_STAGES)
    )
    # Backend-coupled aggregate stages never belong to a single video's chain.
    stage_list = [s for s in stage_list if s not in BACKEND_COUPLED_STAGES]

    def report(stage: str, clip_id: str | None, status: str, **extra: Any) -> None:
        if progress_cb is not None:
            try:
                progress_cb(stage, clip_id, status, extra)
            except Exception as exc:  # progress must never kill the pipeline
                log.warning("progress_cb_failed", stage=stage, error=str(exc))

    ctx = PipelineContext(
        video_id=video_id,
        input_uri=input_uri,
        priority=priority,
        job_id=job_id,
        roster=roster or [],
    )
    stage_timings: dict[str, float] = {}
    failed_clip_stages: list[dict[str, str]] = []

    log.info(
        "pipeline_start",
        video_id=video_id,
        input_uri=input_uri,
        mode="same_session" if is_same_session else "nightly",
        stages=stage_list,
    )

    # Materialise the source once; every stage reuses this local copy.
    ctx.video_path = storage.download_to_temp(input_uri)
    try:
        for stage in [s for s in stage_list if s in VIDEO_STAGES]:
            if should_skip_stage(stage, priority):
                report(stage, None, "skipped", reason="same_session_lightweight_path")
                continue
            cached = sink.load(stage)
            if cached is not None:
                _fold_cached_video_stage(stage, ctx, cached)
                report(stage, None, "succeeded", resumed=True)
                continue
            report(stage, None, "started")
            t0 = time.monotonic()
            try:
                artifacts = _run_video_stage(stage, ctx)
            except Exception:
                report(stage, None, "failed")
                raise
            stage_timings[stage] = round(time.monotonic() - t0, 3)
            ctx.stage_artifacts[stage] = artifacts
            _merge_routing(ctx, artifacts)
            sink.write(stage, artifacts)
            report(stage, None, "succeeded", **_stage_headline(stage, artifacts))

        clip_stage_list = [s for s in stage_list if s in CLIP_STAGES]
        for clip in ctx.clips:
            clip_id = str(clip["id"])
            for stage in clip_stage_list:
                if should_skip_stage(stage, priority):
                    report(stage, clip_id, "skipped", reason="same_session_lightweight_path")
                    continue
                cached = sink.load(stage, clip_id)
                if cached is not None:
                    ctx.clip_artifacts.setdefault(clip_id, {})[stage] = cached
                    report(stage, clip_id, "succeeded", resumed=True)
                    continue
                report(stage, clip_id, "started")
                t0 = time.monotonic()
                try:
                    artifacts = _run_clip_stage(stage, ctx, clip)
                except Exception as exc:
                    # A single clip's stage failure should not sink the whole
                    # video — record it and continue with the next clip.
                    log.error(
                        "clip_stage_failed",
                        stage=stage,
                        clip_id=clip_id,
                        error=str(exc),
                    )
                    failed_clip_stages.append(
                        {"clip_id": clip_id, "stage": stage, "error": str(exc)}
                    )
                    report(stage, clip_id, "failed", error=str(exc))
                    break  # later stages for this clip depend on this one
                key = f"{stage}:{clip_id}"
                stage_timings[key] = round(time.monotonic() - t0, 3)
                ctx.clip_artifacts.setdefault(clip_id, {})[stage] = artifacts
                _merge_routing(ctx, artifacts)
                sink.write(stage, artifacts, clip_id)
                report(stage, clip_id, "succeeded", **_stage_headline(stage, artifacts))
    finally:
        if ctx.video_path is not None:
            ctx.video_path.unlink(missing_ok=True)

    summary: dict[str, Any] = {
        "video_id": video_id,
        "pipeline_mode": "same_session" if is_same_session else "nightly",
        "capture_regime": ctx.capture_regime,
        "regime_confidence": ctx.regime_confidence,
        "analytics_safe": ctx.analytics_safe,
        "fps": ctx.fps,
        "clip_count": len(ctx.clips),
        "clips": [
            {
                "id": str(c["id"]),
                "start_time": c.get("start_time"),
                "end_time": c.get("end_time"),
                "play_number": c.get("play_number"),
                "tracklet_count": ctx.clip_artifacts.get(str(c["id"]), {})
                .get("track", {})
                .get("tracklet_count", 0),
                "event_count": ctx.clip_artifacts.get(str(c["id"]), {})
                .get("events", {})
                .get("event_count", 0),
                "overlay_uri": ctx.clip_artifacts.get(str(c["id"]), {})
                .get("render", {})
                .get("overlay_uri")
                or ctx.clip_artifacts.get(str(c["id"]), {})
                .get("render", {})
                .get("period_overlay_uri"),
            }
            for c in ctx.clips
        ],
        "model_routing": ctx.model_routing,
        "stage_timings": stage_timings,
        "failed_clip_stages": failed_clip_stages,
        "duration_seconds": round(time.monotonic() - started, 2),
    }
    sink.write("summary", summary)
    log.info(
        "pipeline_done",
        video_id=video_id,
        clip_count=summary["clip_count"],
        failed_clip_stages=len(failed_clip_stages),
        duration_seconds=summary["duration_seconds"],
    )
    return summary


def _fold_cached_video_stage(
    stage: str, ctx: PipelineContext, cached: dict[str, Any]
) -> None:
    """Rehydrate context fields from a resumed stage's ledger snapshot."""
    ctx.stage_artifacts[stage] = cached
    _merge_routing(ctx, cached)
    if stage == "ingest":
        ctx.capture_regime = cached.get("capture_regime") or ctx.capture_regime
        ctx.regime_confidence = float(cached.get("regime_confidence") or 0.0)
        probe = cached.get("probe") or {}
        if probe.get("fps"):
            ctx.fps = float(probe["fps"])
    elif stage == "segment":
        ctx.clips = [c for c in cached.get("clips", []) if c.get("id")]
    elif stage == "calibrate":
        ctx.analytics_safe = bool(cached.get("analytics_safe", False))
        ctx.calibration = cached
    elif stage == "detect":
        ctx.detections = cached.get("detections", {})
        if cached.get("fps"):
            ctx.fps = float(cached["fps"])


def _merge_routing(ctx: PipelineContext, artifacts: dict[str, Any]) -> None:
    routing = artifacts.get("model_routing")
    if isinstance(routing, dict):
        for k, v in routing.items():
            ctx.model_routing.setdefault(str(k), str(v))


def _stage_headline(stage: str, artifacts: dict[str, Any]) -> dict[str, Any]:
    """Small, human-scannable per-stage numbers for progress reporting."""
    keys = (
        "clip_count",
        "tracklet_count",
        "event_count",
        "label_count",
        "metric_count",
        "keypoint_count",
        "capture_regime",
        "analytics_safe",
        "total_frames",
    )
    return {k: artifacts[k] for k in keys if k in artifacts}
