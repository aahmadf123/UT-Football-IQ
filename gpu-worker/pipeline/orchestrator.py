"""Pipeline orchestrator — walks a video through every processing stage.

This is the missing spine between "a raw video file / storage URI" and
"clips with detections, tracklets, events, labels, metrics, and rendered
overlays". Historically each stage ran as its own queue message and nothing
chained them; artifacts were expected to ride inside queue payloads (which
would blow past any message-broker payload cap on real footage). Here the stages run
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

from pipeline.checkpoints import atomic_write, checkpoint_identity, envelope, read_envelope
from pipeline.runtime import PipelineInterrupted, check_control, set_checkpoint_identity

log = structlog.get_logger(__name__)

#: Stages that operate on the whole video, in canonical order.
VIDEO_STAGES: tuple[str, ...] = ("ingest", "segment", "calibrate", "detect")

#: Stages that operate per clip, in canonical order.
#:
#: ``events`` precedes ``pose`` and ``labels`` because both consume the snap it
#: detects: ``stage_pose`` anchors its biomechanics at the snap frame and
#: ``stage_labels`` reads formation at the snap. With ``pose`` running first it
#: was handed an events list that was always empty, so every pose metric was
#: computed against no snap at all.
#:
#: The reverse dependency is real but weaker: ``stage_events`` can take
#: ``pose_by_frame`` as one of five snap signals. It is optional, ``stage_pose``
#: does not currently return it, and the other four signals do not need it --
#: so events goes first. Feeding pose back in would need a second pass.
CLIP_STAGES: tuple[str, ...] = (
    "playback_proxy",
    "track",
    "reid",
    "events",
    "pose",
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
BACKEND_COUPLED_STAGES: frozenset[str] = frozenset({"self_scout", "embeddings", "workload_rollup"})

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

    def configure(self, identity: dict[str, Any]) -> None:
        self.fingerprint = str(identity["fingerprint"])

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
        self.fingerprint = "unconfigured"

    def _path(self, stage: str, clip_id: str | None) -> Path:
        if any(part in stage for part in ("/", "\\", "..")) or (
            clip_id and any(part in clip_id for part in ("/", "\\", ".."))
        ):
            raise ValueError("Invalid checkpoint stage or clip identifier")
        name = f"{stage}.{clip_id}.json" if clip_id else f"{stage}.json"
        return self.root / name

    def write(self, stage: str, payload: dict[str, Any], clip_id: str | None = None) -> str | None:
        path = self._path(stage, clip_id)
        atomic_write(path, envelope(payload, self.fingerprint, _json_default))
        return str(path)

    def load(self, stage: str, clip_id: str | None = None) -> dict[str, Any] | None:
        path = self._path(stage, clip_id)
        if not path.is_file():
            return None
        try:
            return read_envelope(path.read_bytes(), self.fingerprint)
        except (OSError, json.JSONDecodeError):
            return None


class StorageArtifactSink(ArtifactSink):
    """Writes ledger JSON through the storage facade (s3:// or local://)."""

    def __init__(self, video_id: str) -> None:
        self.video_id = video_id
        self.fingerprint = "unconfigured"

    def _key(self, stage: str, clip_id: str | None) -> str:
        name = f"{stage}.{clip_id}.json" if clip_id else f"{stage}.json"
        return f"pipeline_artifacts/{self.video_id}/{self.fingerprint}/{name}"

    def write(self, stage: str, payload: dict[str, Any], clip_id: str | None = None) -> str | None:
        from pipeline import storage

        try:
            data = envelope(payload, self.fingerprint, _json_default)
            uri = storage.upload_bytes(
                data, self._key(stage, clip_id), content_type="application/json"
            )
            return uri
        except Exception as exc:
            log.error("artifact_ledger_write_failed", stage=stage, error=str(exc))
            raise

    def load(self, stage: str, clip_id: str | None = None) -> dict[str, Any] | None:
        from pipeline import storage

        # Resolve the deterministic object URI even in a fresh worker process.
        scheme = storage.active_backend()
        if scheme == "api":
            ref = f"api://artifacts/{self._key(stage, clip_id)}"
        else:
            ref = f"{scheme}://artifacts/{self._key(stage, clip_id)}"
        try:
            path = storage.download_to_temp(ref)
        except Exception:
            return None
        try:
            return read_envelope(path.read_bytes(), self.fingerprint)
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
    processing_profile: str = "quick"

    # Populated by video-scoped stages
    video_path: Path | None = None  # master local copy, downloaded once
    fps: float = 30.0
    capture_regime: str | None = None
    regime_confidence: float = 0.0
    analytics_safe: bool = False
    calibration: dict[str, Any] = field(default_factory=dict)
    #: Flat 9-element row-major homography from the calibrate stage, mapping
    #: pixels to field yards. Kept as a plain list so it survives the artifact
    #: sink's JSON round-trip on resume; call sites normalise it with
    #: ``homography.homography_from_flat`` before doing any matrix maths.
    homography: list[float] | None = None
    #: Serialised field boundary from calibrate. Populated in both the live and
    #: the cached-fold branches -- calibrate runs before detect, so a resumed
    #: run that lost it would silently stop filtering off-field people.
    field_boundary: dict[str, Any] | None = None
    detections: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    clips: list[dict[str, Any]] = field(default_factory=list)

    # Per-stage outputs: stage → artifacts (video-scoped) and
    # clip_id → stage → artifacts (clip-scoped)
    stage_artifacts: dict[str, dict[str, Any]] = field(default_factory=dict)
    clip_artifacts: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    model_routing: dict[str, str] = field(default_factory=dict)
    model_provenance: dict[str, Any] = field(default_factory=dict)
    calibration_series: list[dict[str, Any]] = field(default_factory=list)
    effective_annotations: dict[str, dict[str, Any]] = field(default_factory=dict)
    lineage: dict[str, Any] = field(default_factory=dict)
    timeline: Any = None


# ── Per-clip helpers ──────────────────────────────────────────────────────────


def _clip_frame_window(clip: dict[str, Any], fps: float, timeline: Any = None) -> tuple[int, int]:
    start_t = float(clip.get("start_time") or 0.0)
    end_t = float(clip.get("end_time") or 0.0)
    if timeline is not None:
        return timeline.window(start_t, end_t)
    return int(start_t * fps), max(int(end_t * fps), int(start_t * fps))


def _ball_only(
    detections: dict[str, list[dict[str, Any]]],
) -> dict[str, list[dict[str, Any]]] | None:
    """Split the ball out of the per-frame detection map.

    ``stage_detect`` merges the dedicated ball model's output into the same
    frame-keyed dict as the players, tagged ``class="ball"``, but
    ``stage_events`` wants the ball on its own -- it fits a trajectory to it and
    drives the snap/throw/catch state machine from that.

    Returns ``None`` when no ball was detected anywhere in the clip, which is
    the signal the stage already understands for "run without the ball track".
    """
    ball: dict[str, list[dict[str, Any]]] = {}
    for frame, dets in detections.items():
        hits = [d for d in dets if d.get("class") == "ball"]
        if hits:
            ball[frame] = hits
    return ball or None


def _slice_detections(
    detections: dict[str, list[dict[str, Any]]], start_frame: int, end_frame: int
) -> dict[str, list[dict[str, Any]]]:
    return {f: dets for f, dets in detections.items() if start_frame <= int(f) < end_frame}


def _offense_direction(labels: list[dict[str, Any]]) -> int | None:
    """The measured attacking direction, or ``None`` when it was not resolved.

    ``None`` is the point of this function. It used to fall back to ``+1``,
    which is a coin call on a quantity that flips the sign of every
    depth-behind-LOS feature: get it wrong and the coverage graph has the
    offence and the defence swapped, with nothing in the output to say so.
    Callers must decide what an unresolved orientation means for them.
    """
    for lbl in labels:
        if lbl.get("label_type") == "field_orientation":
            direction = (lbl.get("label_value") or {}).get("direction")
            if direction in (1, -1):
                return int(direction)
            return None
    return None


# ── Stage runners ─────────────────────────────────────────────────────────────


def _run_video_stage(stage: str, ctx: PipelineContext) -> dict[str, Any]:
    """Run one video-scoped stage and fold its outputs into the context."""
    from pipeline import model_router
    from pipeline.lightweight_config import local_model_variant

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
        # Coach-edited cuts are authoritative. Reprocessing may refresh
        # analytics, but must not replace the reviewed play layout.
        existing = ctx.lineage.get("existing_clips") or []
        if existing and any(clip.get("boundary_source") == "manual" for clip in existing):
            return {
                "clips": existing,
                "clip_count": len(existing),
                "boundary_model": "coach-reviewed-layout",
                "data_source": "manual",
            }
        from pipeline import stage_segment

        artifacts = stage_segment.run(
            ctx.video_id, local_uri, ctx.job_id, priority=ctx.priority, timeline=ctx.timeline,
            processing_profile=ctx.processing_profile,
        )
        ctx.clips = [c for c in artifacts.get("clips", []) if c.get("id")]
        return artifacts

    if stage == "calibrate":
        from pipeline import stage_calibrate

        variant = local_model_variant("calibrate", ctx.priority, ctx.processing_profile)
        artifacts = stage_calibrate.run(
            ctx.video_id,
            local_uri,
            ctx.job_id,
            variant=variant,
            capture_regime=ctx.capture_regime,
            timeline=ctx.timeline,
        )
        ctx.analytics_safe = bool(artifacts.get("analytics_safe", False))
        ctx.calibration = artifacts
        ctx.homography = artifacts.get("homography")
        ctx.field_boundary = artifacts.get("field_boundary")
        ctx.calibration_series = artifacts.get("calibration_series") or []
        # A global transform is unsafe when the camera moves. Per-frame track
        # projection below resolves the timeline independently.
        if ctx.capture_regime not in {"fixed_sideline", "fixed_endzone"}:
            ctx.homography = None
            if not ctx.calibration_series:
                ctx.analytics_safe = False
        return artifacts

    if stage == "detect":
        from pipeline import stage_detect

        variant = local_model_variant("detect", ctx.priority, ctx.processing_profile)
        if __import__("os").environ.get("PIPELINE_DEVICE", "cpu") != "cpu":
            variant = model_router.select_detect_variant(ctx.priority, ctx.capture_regime)
        artifacts = stage_detect.run(
            ctx.video_id,
            local_uri,
            ctx.job_id,
            variant=variant,
            capture_regime=ctx.capture_regime,
            priority=ctx.priority,
            field_boundary=ctx.field_boundary,
            timeline=ctx.timeline,
        )
        ctx.detections = artifacts.get("detections", {})
        if artifacts.get("fps"):
            ctx.fps = float(artifacts["fps"])
        return artifacts

    raise ValueError(f"Unknown video-scoped stage: {stage}")


def _uncertainty_heads(artifacts: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Pull each classifier head's entropy out of the stage artifacts.

    Heads that ran but could not calibrate report ``uncertainty: None`` -- there
    is no distribution to take the entropy of, so they contribute nothing rather
    than a zero, which would read as "completely certain" and sink the clip to
    the bottom of the review queue.
    """
    from pipeline.calibration.calibrated_output import CalibratedOutput

    heads: dict[str, Any] = {}
    sources = {
        "coverage": artifacts.get("coverage") or {},
        # stage_pressure nests its CalibratedOutput under the metric it writes.
        "pressure": (artifacts.get("pressure") or {}).get("pressure") or {},
    }
    for name, source in sources.items():
        entropy = source.get("uncertainty")
        if entropy is None:
            continue
        heads[name] = CalibratedOutput(
            value=name,
            confidence=_head_confidence(source),
            is_calibrated=bool(source.get("is_calibrated")),
            entropy=float(entropy),
        )
    return heads


def _head_confidence(source: dict[str, Any]) -> float:
    """The head's confidence, under whichever key that stage happens to use.

    Explicit ``is None`` rather than ``a or b``: a head that legitimately
    reports ``0.0`` -- no confidence at all, which is a real answer -- would
    otherwise be treated as absent and silently replaced by the next key.
    """
    for key in ("confidence", "coverage_confidence"):
        value = source.get(key)
        if value is not None:
            return float(value)
    return 0.0


def _score_clip_uncertainty(ctx: PipelineContext, clip_id: str) -> None:
    """Reduce the clip's heads to one review-queue priority and write it back."""
    from pipeline import backend
    from pipeline.calibration import clip_uncertainty

    heads = _uncertainty_heads(ctx.clip_artifacts.get(clip_id, {}))
    result = clip_uncertainty(heads)
    log.info(
        "clip_uncertainty_scored",
        clip_id=clip_id,
        score=result.score,
        calibrated=result.is_calibrated,
        contributing=list(result.contributing),
    )
    backend.patch_clip_uncertainty(clip_id, result.score, result.is_calibrated)


def _run_clip_stage(stage: str, ctx: PipelineContext, clip: dict[str, Any]) -> dict[str, Any]:
    """Run one clip-scoped stage for ``clip`` and fold outputs into context."""
    from pipeline.lightweight_config import local_model_variant

    clip_id = str(clip["id"])
    per_clip = ctx.clip_artifacts.setdefault(clip_id, {})
    tracklets: list[dict[str, Any]] = per_clip.get("track", {}).get("tracklets", [])
    events_list: list[dict[str, Any]] = per_clip.get("events", {}).get("events", [])
    labels_list: list[dict[str, Any]] = per_clip.get("labels", {}).get("labels", [])
    metrics_list: list[dict[str, Any]] = per_clip.get("metrics", {}).get("metrics", [])
    pose_metrics: dict[str, Any] = per_clip.get("pose", {}).get("pose_metrics", {})
    start_frame, end_frame = _clip_frame_window(clip, ctx.fps, ctx.timeline)
    analytics_safe = ctx.analytics_safe
    if ctx.calibration_series and tracklets:
        points = [point for track in tracklets for point in track.get("track_points", [])]
        analytics_safe = bool(points) and all(
            isinstance(point.get("field_x"), (int, float))
            and isinstance(point.get("field_y"), (int, float))
            for point in points
        )

    if stage == "track":
        from pipeline import stage_track

        variant = local_model_variant("track", ctx.priority, ctx.processing_profile)
        clip_dets = _slice_detections(ctx.detections, start_frame, end_frame)
        return stage_track.run(
            clip_id,
            clip_dets,
            ctx.fps,
            ctx.job_id,
            variant=variant,
            # Field coordinates are attached here, at the one place that sees
            # both the tracks and the calibration. Every downstream spatial
            # metric reads them off the track points.
            homography=ctx.homography,
            analytics_safe=analytics_safe,
            calibration_series=ctx.calibration_series or None,
            capture_regime=ctx.capture_regime,
            detector_provenance=ctx.model_provenance.get("detect"),
        )

    if stage == "reid":
        from pipeline import stage_reid
        from pipeline.backend import BACKEND_API_URL

        if ctx.video_path is None:
            return {"reid_skipped": True, "reason": "no_video_path"}
        variant = local_model_variant("reid", ctx.priority, ctx.processing_profile)
        return stage_reid.run(
            clip_id,
            ctx.video_path,
            [t["id"] for t in tracklets],
            tracklets,
            ctx.roster,
            BACKEND_API_URL,
            variant=variant,
            priority=ctx.priority,
            timeline=ctx.timeline,
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
                analytics_safe,
                ctx.fps,
                ctx.job_id,
                model_path,
                frame_start=start_frame,
                frame_end=end_frame,
            )

    if stage == "events":
        from pipeline import stage_events

        clip_dets = _slice_detections(ctx.detections, start_frame, end_frame)
        # `homography` and `ball_detections` are the two that matter. Without
        # them the stage skips its entire ball state machine, so no throw, catch
        # or interception can be emitted and only `snap` survives -- which is
        # why event_count was 0 on every archived baseline clip.
        #
        # The remaining optional inputs stay unset on purpose. `pose_by_frame`
        # would need stage_pose to return it (it does not) and pose now runs
        # after this stage anyway; the OL/QB/centre track ids would come from
        # stage_labels, which consumes events and therefore cannot precede them.
        # Both are genuine ordering constraints, not oversights -- see the note
        # on CLIP_STAGES.
        return stage_events.run(
            clip_id,
            clip_dets,
            ctx.fps,
            ctx.job_id,
            tracklets=tracklets or None,
            ball_detections=_ball_only(clip_dets),
            homography=ctx.homography,
            timeline=ctx.timeline,
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
            analytics_safe,
            ctx.fps,
            ctx.job_id,
            pose_metrics=pose_metrics or None,
            timeline=ctx.timeline,
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
            analytics_safe=analytics_safe,
            offense_direction=_offense_direction(labels_list),
        )

    if stage == "oline":
        from pipeline import stage_oline

        return stage_oline.run(clip_id, tracklets, events_list, analytics_safe, ctx.fps, ctx.job_id)

    if stage == "pressure":
        from pipeline import stage_pressure

        return stage_pressure.run(
            clip_id,
            tracklets,
            events_list,
            analytics_safe,
            ctx.fps,
            ctx.job_id,
            offense_direction=_offense_direction(labels_list),
            down=clip.get("down"),
            distance_yards=clip.get("distance"),
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
                analytics_safe,
                ctx.fps,
                frame_start=start_frame,
                frame_end=end_frame,
                timeline=ctx.timeline,
                start_seconds=clip.get("start_time"),
                end_seconds=clip.get("end_time"),
            )
        return stage_render.run(
            clip_id,
            ctx.video_path,
            tracklets,
            labels_list,
            metrics_list,
            analytics_safe,
            ctx.fps,
            BACKEND_API_URL,
            frame_start=start_frame,
            frame_end=end_frame,
            timeline=ctx.timeline,
            start_seconds=clip.get("start_time"),
            end_seconds=clip.get("end_time"),
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
    processing_profile: str | None = None,
    revisions: dict[str, Any] | None = None,
    lineage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Process film locally, keeping compatible completed stages after restart.

    Profiles choose capabilities independently of queue priority. Every
    checkpoint validates actual film/model/config/annotation lineage. Optional
    analysis can be unavailable while imported film and completed clips remain
    usable; the returned summary states those failures explicitly.
    """
    from pipeline import model_registry_client, model_router, storage
    from pipeline.lightweight_config import (
        processing_profile as resolve_profile,
    )
    from pipeline.lightweight_config import (
        profile_stages,
        should_skip_stage,
    )

    started = time.monotonic()
    sink = artifact_sink or NullArtifactSink()
    profile = resolve_profile(processing_profile)
    is_same_session = model_router.is_same_session(priority)
    stage_list = list(stages if stages is not None else profile_stages(profile))
    stage_list = [stage for stage in stage_list if stage not in BACKEND_COUPLED_STAGES]
    unknown = set(stage_list).difference(VIDEO_STAGES, CLIP_STAGES)
    if unknown:
        raise ValueError(f"Unknown processing stages: {', '.join(sorted(unknown))}")

    ctx = PipelineContext(
        video_id=video_id,
        input_uri=input_uri,
        priority=priority,
        job_id=job_id,
        roster=roster or [],
        processing_profile=profile,
        effective_annotations={
            str(item["id"]): item for item in (revisions or {}).get("effective_annotations", [])
        },
        lineage=lineage or {},
    )
    stage_timings: dict[str, float] = {}
    stage_states: dict[str, dict[str, Any]] = {}
    failed_clip_stages: list[dict[str, str]] = []
    failed_video_stages: list[dict[str, str]] = []
    identity: dict[str, Any] = {}
    interrupted: str | None = None

    def report(stage: str, clip_id: str | None, status: str, **extra: Any) -> None:
        key = f"{stage}:{clip_id}" if clip_id else stage
        stage_states[key] = {"status": status, **extra}
        if progress_cb is not None:
            try:
                progress_cb(stage, clip_id, status, extra)
            except PipelineInterrupted:
                raise
            except Exception as exc:
                log.warning("progress_cb_failed", stage=stage, error=str(exc))

    def accept(stage: str, clip_id: str | None, artifacts: dict[str, Any]) -> None:
        if clip_id is None:
            _fold_cached_video_stage(stage, ctx, artifacts)
            if stage == "segment":
                for clip in ctx.clips:
                    corrected = ctx.effective_annotations.get(str(clip["id"]))
                    if corrected:
                        clip.update(corrected)
        else:
            per_clip = ctx.clip_artifacts.setdefault(clip_id, {})
            per_clip[stage] = artifacts
            if stage == "events":
                corrected = ctx.effective_annotations.get(clip_id, {})
                for original in corrected.get("human_events") or []:
                    event = {
                        **original,
                        "attributes": dict(original.get("attributes") or {}),
                        "source": "human",
                    }
                    # Staff event entry uses the play clock. Pipeline consumers
                    # use absolute recording frames consistently, including pose.
                    timebase = event["attributes"].get("timebase", "unknown")
                    events = artifacts.setdefault("events", [])
                    if timebase == "unknown":
                        events[:] = [
                            item for item in events if item.get("event_type") != event["event_type"]
                        ]
                        artifacts.setdefault("event_review_required", []).append(
                            {"event_id": event.get("id"), "reason": "event_timebase_unknown"}
                        )
                        continue
                    if timebase == "clip":
                        offset = float(corrected.get("start_time") or 0)
                        if event.get("timestamp_seconds") is not None:
                            event["timestamp_seconds"] = offset + float(event["timestamp_seconds"])
                            event["frame_number"] = (
                                ctx.timeline.frame_for_time(event["timestamp_seconds"])
                                if ctx.timeline
                                else int(event["timestamp_seconds"] * ctx.fps)
                            )
                        elif event.get("frame_number") is not None:
                            event["frame_number"] = (
                                ctx.timeline.frame_for_time(offset)
                                if ctx.timeline
                                else int(offset * ctx.fps)
                            ) + int(event["frame_number"])
                            if ctx.timeline:
                                event["timestamp_seconds"] = ctx.timeline.timestamp(
                                    event["frame_number"]
                                )
                        event["attributes"]["timebase"] = "recording"
                    elif timebase == "recording" and ctx.timeline:
                        if event.get("timestamp_seconds") is not None:
                            event["frame_number"] = ctx.timeline.frame_for_time(
                                event["timestamp_seconds"]
                            )
                        elif event.get("frame_number") is not None:
                            event["timestamp_seconds"] = ctx.timeline.timestamp(
                                event["frame_number"]
                            )
                    events = artifacts.setdefault("events", [])
                    events[:] = [
                        item for item in events if item.get("event_type") != event["event_type"]
                    ]
                    events.append(event)
                artifacts["event_count"] = len(artifacts.get("events") or [])
            if stage == "labels":
                corrected = ctx.effective_annotations.get(clip_id, {}).get("label_data") or {}
                for label_type, label_value in corrected.items():
                    if label_type.startswith("_") or not isinstance(label_value, dict):
                        continue
                    labels = artifacts.setdefault("labels", [])
                    labels[:] = [item for item in labels if item.get("label_type") != label_type]
                    labels.append(
                        {"label_type": label_type, "label_value": label_value, "source": "human"}
                    )
            if stage == "coverage" and isinstance(artifacts.get("feature_packet"), dict):
                packet = artifacts["feature_packet"]
                packet.update(
                    {
                        key: value
                        for key, value in ctx.lineage.items()
                        if key in {"session_id", "recording_id", "split"}
                    }
                )
                current_clip = next(
                    (
                        item
                        for item in ctx.lineage.get("existing_clips", [])
                        if str(item["id"]) == clip_id
                    ),
                    {},
                )
                explicit_split = (current_clip.get("label_data") or {}).get("_learning_split")
                if explicit_split:
                    packet["split"] = explicit_split
                actual_clip = next((item for item in ctx.clips if str(item["id"]) == clip_id), {})
                packet["source_start_seconds"] = actual_clip.get("start_time")
                packet["source_end_seconds"] = actual_clip.get("end_time")
                effective_data = (
                    ctx.effective_annotations.get(clip_id, {}).get("label_data")
                    or current_clip.get("label_data")
                    or {}
                )
                packet["source_observation_revision"] = int(
                    effective_data.get("_observation_revision") or 0
                )
                packet["source_annotation_revision"] = int(
                    ctx.effective_annotations.get(clip_id, {}).get("annotation_revision") or 0
                )
                packet["source_calibration_fingerprint"] = (
                    __import__("hashlib")
                    .sha256(
                        json.dumps(ctx.calibration, default=_json_default, sort_keys=True).encode()
                    )
                    .hexdigest()
                )
                detector_source = (ctx.model_provenance.get("detect") or {}).get("source")
                if detector_source != "real":
                    packet["source"] = detector_source or "unvalidated"
                    packet["analytics_safe"] = False
            # Identity and classification enrichments must reach metrics and
            # rendering in both live execution and a resumed run.
            enriched = artifacts.get("tracklets")
            if stage in {"reid", "labels"} and isinstance(enriched, list):
                per_clip.setdefault("track", {})["tracklets"] = enriched
            _merge_routing(ctx, artifacts)

    def stage_status(artifacts: dict[str, Any]) -> str:
        return (
            "skipped"
            if artifacts.get("skipped")
            or any(value is True for key, value in artifacts.items() if key.endswith("_skipped"))
            else "succeeded"
        )

    playback_prepared = False

    def prepare_playback() -> None:
        nonlocal playback_prepared
        if playback_prepared or "playback_proxy" not in stage_list or not ctx.clips:
            return
        from pipeline.playback_proxy import PlaybackProxySession

        playback_prepared = True
        session = PlaybackProxySession(
            ctx.video_path, ctx.timeline, source_sha256=identity.get("source_sha256")
        )
        try:
            for clip in sorted(ctx.clips, key=lambda item: float(item.get("start_time") or 0)):
                check_control()
                clip_id = str(clip["id"])
                stage = "playback_proxy"
                cached = sink.load(stage, clip_id)
                if cached is not None:
                    path = storage.resolve_readable_path(cached.get("playback_uri", ""))
                    if path is None or path.is_file():
                        accept(stage, clip_id, cached)
                        report(stage, clip_id, "succeeded", resumed=True)
                        continue
                # Label/model changes do not alter clean media. Reuse the
                # accepted proxy when the actual source window still matches.
                previous = next(
                    (
                        item
                        for item in ctx.lineage.get("existing_clips", [])
                        if str(item["id"]) == clip_id
                    ),
                    {},
                )
                media = (previous.get("provenance") or {}).get("playback") or {}
                existing_uri = previous.get("storage_uri")
                if (
                    existing_uri
                    and media.get("kind") == "clean_clip"
                    and media.get("source_sha256") == identity.get("source_sha256")
                    and media.get("source_start_seconds") == clip.get("start_time")
                    and media.get("source_end_seconds") == clip.get("end_time")
                ):
                    path = storage.resolve_readable_path(existing_uri)
                    if path is None or path.is_file():
                        artifacts = {"playback_uri": existing_uri, "playback": media}
                        accept(stage, clip_id, artifacts)
                        sink.write(stage, artifacts, clip_id)
                        report(stage, clip_id, "succeeded", reused_media=True)
                        continue
                report(stage, clip_id, "started")
                stage_start = time.monotonic()
                try:
                    artifacts = session.run(
                        clip_id, float(clip.get("start_time") or 0), float(clip["end_time"])
                    )
                    accept(stage, clip_id, artifacts)
                    sink.write(stage, artifacts, clip_id)
                    stage_timings[f"{stage}:{clip_id}"] = round(time.monotonic() - stage_start, 3)
                    report(stage, clip_id, "succeeded")
                except PipelineInterrupted:
                    raise
                except Exception as exc:
                    failed_clip_stages.append(
                        {"clip_id": clip_id, "stage": stage, "error": str(exc)}
                    )
                    report(stage, clip_id, "failed", error=str(exc))
        finally:
            session.close()

    def summary() -> dict[str, Any]:
        errors = failed_video_stages + failed_clip_stages
        source_values = {
            artifacts.get("data_source") for artifacts in ctx.stage_artifacts.values()
        } | {
            entry.get("source")
            for entry in ctx.model_provenance.values()
            if isinstance(entry, dict)
        }
        source = (
            "synthetic_test"
            if source_values & {"synthetic", "synthetic_test", "stub"}
            else "real"
            if "real" in source_values
            else "manual"
            if profile == "review"
            else "unvalidated"
        )
        return {
            "video_id": video_id,
            "pipeline_mode": "same_session" if is_same_session else "nightly",
            "processing_profile": profile,
            "result_state": "interrupted" if interrupted else "partial" if errors else "complete",
            "interrupted_action": interrupted,
            "source": source,
            "trusted_analysis": source == "real"
            and not errors
            and bool((ctx.model_provenance.get("detect") or {}).get("validated", False)),
            "checkpoint": identity,
            "capture_regime": ctx.capture_regime,
            "regime_confidence": ctx.regime_confidence,
            "analytics_safe": ctx.analytics_safe and source == "real",
            "fps": ctx.fps,
            "clip_count": len(ctx.clips),
            "playback_ready": any(
                (outputs.get("playback_proxy") or {}).get("playback_uri")
                for outputs in ctx.clip_artifacts.values()
            )
            or (ctx.stage_artifacts.get("ingest", {}).get("probe", {}).get("codec_name") == "h264"),
            "clips": [
                {
                    "id": str(clip["id"]),
                    "start_time": clip.get("start_time"),
                    "end_time": clip.get("end_time"),
                    "play_number": clip.get("play_number"),
                    "tracklet_count": ctx.clip_artifacts.get(str(clip["id"]), {})
                    .get("track", {})
                    .get("tracklet_count", 0),
                    "event_count": ctx.clip_artifacts.get(str(clip["id"]), {})
                    .get("events", {})
                    .get("event_count", 0),
                    "source_start_time_seconds": clip.get("start_time"),
                    "overlay_start_time_seconds": 0,
                    "playback_uri": (
                        ctx.clip_artifacts.get(str(clip["id"]), {}).get("playback_proxy") or {}
                    ).get("playback_uri"),
                    "proxy_timeline": (
                        ctx.clip_artifacts.get(str(clip["id"]), {}).get("playback_proxy") or {}
                    ).get("proxy_timeline"),
                    "overlay_proxy_timeline": (
                        ctx.clip_artifacts.get(str(clip["id"]), {}).get("render") or {}
                    ).get("proxy_timeline"),
                    "overlay_uri": ctx.clip_artifacts.get(str(clip["id"]), {})
                    .get("render", {})
                    .get("overlay_uri")
                    or ctx.clip_artifacts.get(str(clip["id"]), {})
                    .get("render", {})
                    .get("period_overlay_uri"),
                }
                for clip in ctx.clips
            ],
            "model_routing": ctx.model_routing,
            "model_provenance": ctx.model_provenance,
            "stage_states": stage_states,
            "stage_timings": stage_timings,
            "failed_video_stages": failed_video_stages,
            "failed_clip_stages": failed_clip_stages,
            "duration_seconds": round(time.monotonic() - started, 2),
        }

    check_control()
    ctx.video_path = storage.download_to_temp(input_uri)
    try:
        from pipeline.hosted_limits import validate_master

        validate_master(ctx.video_path)
        from pipeline.video_timeline import VideoTimeline

        ctx.timeline = VideoTimeline.from_path(ctx.video_path, fps_fallback=ctx.fps)
        model_registry_client.reset_cache()
        model_lineage = model_registry_client.resolved_identity()
        revision_inputs = {**(revisions or {}), "production_models": model_lineage}
        identity = checkpoint_identity(
            ctx.video_path,
            video_id=video_id,
            profile=profile,
            stages=stage_list,
            priority=priority,
            revisions={**revision_inputs, "timestamps_verified": ctx.timeline.verified},
        )
        sink.configure(identity)
        set_checkpoint_identity(
            str(identity["fingerprint"]), f"{video_id}:{identity['source_sha256']}"
        )
        sink.write("manifest", identity)
        for stage in [name for name in stage_list if name in VIDEO_STAGES]:
            check_control()
            if stage in {"calibrate", "detect"}:
                prepare_playback()
            if should_skip_stage(stage, priority):
                report(stage, None, "skipped", reason="same_session_lightweight_path")
                continue
            cached = sink.load(stage)
            if cached is not None:
                accept(stage, None, cached)
                report(
                    stage,
                    None,
                    stage_status(cached),
                    resumed=True,
                    **_stage_headline(stage, cached),
                )
                continue
            report(stage, None, "started")
            stage_start = time.monotonic()
            try:
                artifacts = _run_video_stage(stage, ctx)
                check_control()
                accept(stage, None, artifacts)
                sink.write(stage, artifacts)
                stage_timings[stage] = round(time.monotonic() - stage_start, 3)
                report(stage, None, stage_status(artifacts), **_stage_headline(stage, artifacts))
            except PipelineInterrupted:
                raise
            except Exception as exc:
                failed_video_stages.append({"stage": stage, "error": str(exc)})
                report(stage, None, "failed", error=str(exc))
                # Imported film and proposed clips still belong to the user.
                # Never run analytics after a required upstream failure.
                break

        prepare_playback()
        clip_stages = [
            name for name in stage_list if name in CLIP_STAGES and name != "playback_proxy"
        ]
        if failed_video_stages:
            for stage in clip_stages:
                report(stage, None, "skipped", reason="upstream_stage_failed")
        else:
            for clip in ctx.clips:
                clip_id = str(clip["id"])
                for stage in clip_stages:
                    check_control()
                    if should_skip_stage(stage, priority):
                        report(stage, clip_id, "skipped", reason="same_session_lightweight_path")
                        continue
                    cached = sink.load(stage, clip_id)
                    if cached is not None:
                        accept(stage, clip_id, cached)
                        report(
                            stage,
                            clip_id,
                            stage_status(cached),
                            resumed=True,
                            **_stage_headline(stage, cached),
                        )
                        continue
                    report(stage, clip_id, "started")
                    stage_start = time.monotonic()
                    try:
                        artifacts = _run_clip_stage(stage, ctx, clip)
                        check_control()
                        accept(stage, clip_id, artifacts)
                        sink.write(stage, artifacts, clip_id)
                        stage_timings[f"{stage}:{clip_id}"] = round(
                            time.monotonic() - stage_start, 3
                        )
                        report(
                            stage,
                            clip_id,
                            stage_status(artifacts),
                            **_stage_headline(stage, artifacts),
                        )
                    except PipelineInterrupted:
                        raise
                    except Exception as exc:
                        failed_clip_stages.append(
                            {
                                "clip_id": clip_id,
                                "stage": stage,
                                "error": str(exc),
                            }
                        )
                        report(stage, clip_id, "failed", error=str(exc))
                        for dependent in clip_stages[clip_stages.index(stage) + 1 :]:
                            report(dependent, clip_id, "skipped", reason="upstream_stage_failed")
                        break
                check_control()
                _score_clip_uncertainty(ctx, clip_id)
    except PipelineInterrupted as exc:
        interrupted = exc.action
    finally:
        if ctx.video_path is not None:
            ctx.video_path.unlink(missing_ok=True)

    result = summary()
    feature_packets = [
        packet
        for artifacts in ctx.clip_artifacts.values()
        if isinstance(packet := (artifacts.get("coverage") or {}).get("feature_packet"), dict)
    ]
    if feature_packets and not interrupted:
        check_control()
        rows = (
            "\n".join(
                json.dumps(packet, default=_json_default, sort_keys=True)
                for packet in feature_packets
            )
            + "\n"
        )
        result["feature_artifact_uri"] = storage.upload_bytes(
            rows.encode(),
            f"training_features/{video_id}/{identity['fingerprint']}.jsonl",
            content_type="application/x-ndjson",
        )
        result["feature_row_count"] = len(feature_packets)
    sink.write("summary", result)
    log.info(
        "pipeline_done",
        video_id=video_id,
        result_state=result["result_state"],
        clip_count=result["clip_count"],
        duration_seconds=result["duration_seconds"],
    )
    return result


def _fold_cached_video_stage(stage: str, ctx: PipelineContext, cached: dict[str, Any]) -> None:
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
        # Mirror of the live branch above. Miss this and a resumed run loses the
        # homography, so every spatial metric silently degrades on exactly the
        # jobs that were interrupted and retried.
        ctx.homography = cached.get("homography")
        ctx.field_boundary = cached.get("field_boundary")
        ctx.calibration_series = cached.get("calibration_series") or []
        if ctx.capture_regime not in {"fixed_sideline", "fixed_endzone"}:
            ctx.homography = None
            if not ctx.calibration_series:
                ctx.analytics_safe = False
    elif stage == "detect":
        ctx.detections = cached.get("detections", {})
        if cached.get("fps"):
            ctx.fps = float(cached["fps"])


def _merge_routing(ctx: PipelineContext, artifacts: dict[str, Any]) -> None:
    routing = artifacts.get("model_routing")
    if isinstance(routing, dict):
        for k, v in routing.items():
            ctx.model_routing[str(k)] = str(v)
    provenance = artifacts.get("model_provenance")
    if isinstance(provenance, dict):
        ctx.model_provenance.update(provenance)


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
        "reason",
        "source",
    )
    return {k: artifacts[k] for k in keys if k in artifacts}
