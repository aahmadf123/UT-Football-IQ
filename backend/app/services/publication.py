"""Promote a complete worker result in the completion transaction."""

import hashlib
import json
import uuid
from collections import defaultdict
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import Clip, ClipResultState, Event, JobType, Label, Metric, ProcessingJob, Tracklet
from app.services.clip_state import active_clip
from app.services.metric_policy import exclusion_reason

PRESERVE_PRIOR_REASONS = {"correction_rebinding_unresolved", "replacement_not_qualified"}


async def publish_job_clip_tiers(db: AsyncSession, job: ProcessingJob) -> None:
    """A detailed badge describes the promoted result, independently of cuts."""
    blocked = await blocked_job_clips(db, job.id)
    tracks = (await db.execute(select(Tracklet).where(Tracklet.job_id == job.id))).scalars().all()
    promoted = {
        track.clip_id
        for track in tracks
        if (track.provenance or {}).get("result_version") == str(job.id)
        and (track.provenance or {}).get("publication_state") != "pending"
        and (track.provenance or {}).get("state") != "superseded"
    }
    metrics = (await db.execute(select(Metric).where(Metric.job_id == job.id))).scalars().all()
    promoted.update(metric.clip_id for metric in metrics if qualified_automatic(metric))
    clips = (
        (await db.execute(select(Clip).where(Clip.id.in_(promoted - set(blocked)), active_clip())))
        .scalars()
        .all()
    )
    profile = (job.input_artifacts or {}).get("processing_profile")
    fast = profile == "quick" or (profile is None and job.priority >= 10)
    for clip in clips:
        clip.result_state = (
            ClipResultState.preliminary.value if fast else ClipResultState.final.value
        )
        clip.provenance = {
            **(clip.provenance or {}),
            "published_result_version": str(job.id),
            "published_processing_profile": "quick" if fast else "detailed",
            "cuts_state": "ready",
        }


async def blocked_job_clips(db: AsyncSession, job_id: uuid.UUID) -> dict[uuid.UUID, str]:
    job = await db.get(ProcessingJob, job_id)
    artifacts = job.output_artifacts if job else None
    stored = artifacts.get("publication_holds", {}) if isinstance(artifacts, dict) else {}
    holds: dict[uuid.UUID, str] = {}
    if isinstance(stored, dict):
        for clip_id, reason in stored.items():
            if not isinstance(reason, str) or reason not in PRESERVE_PRIOR_REASONS:
                continue
            try:
                holds[uuid.UUID(str(clip_id))] = reason
            except ValueError:
                continue
    holds.update(
        {
            clip_id: reason
            for clip_id, reason in (
                await db.execute(
                    select(Tracklet.clip_id, Tracklet.provenance["exclusion_reason"].astext).where(
                        Tracklet.job_id == job_id,
                        Tracklet.provenance["exclusion_reason"].astext.in_(PRESERVE_PRIOR_REASONS),
                    )
                )
            ).all()
        }
    )
    return holds


def qualified_automatic(metric: Metric, *, pending: bool = False) -> bool:
    provenance = metric.provenance or (metric.metric_value or {}).get("provenance") or {}
    return bool(
        metric.analytics_safe
        and not metric.is_suppressed
        and not metric.experimental_flag
        and provenance.get("source") == "real"
        and provenance.get("state") != "superseded"
        and provenance.get("eligibility") != "excluded"
        and (pending or provenance.get("publication_state") != "pending")
    )


async def unqualified_replacement_clips(
    db: AsyncSession,
    job_id: uuid.UUID,
    prior: Sequence[Tracklet],
    candidates: Sequence[Tracklet],
    touched: set[uuid.UUID],
) -> set[uuid.UUID]:
    """An eligible new set is required for every previously qualified actor."""
    old_tracks = {track.id: track for track in prior}
    new_tracks = {track.id: track for track in candidates}
    previous: set[tuple[uuid.UUID, uuid.UUID | None]] = set()
    replacement: set[tuple[uuid.UUID, uuid.UUID | None]] = set()
    metrics = (await db.execute(select(Metric).where(Metric.clip_id.in_(touched)))).scalars().all()
    for metric in metrics:
        is_new = metric.job_id == job_id
        if not qualified_automatic(metric, pending=is_new):
            continue
        tracks = new_tracks if is_new else old_tracks
        player_id = None
        if metric.tracklet_id is not None:
            track = tracks.get(metric.tracklet_id)
            if track is None or track.player_id is None:
                continue
            player_id = track.player_id
        (replacement if is_new else previous).add((metric.clip_id, player_id))
    return {clip_id for clip_id, player_id in previous - replacement}


async def publish_job_events(db: AsyncSession, job_id: uuid.UUID) -> int:
    candidates = (
        (await db.execute(select(Event).where(Event.provenance["job_id"].astext == str(job_id))))
        .scalars()
        .all()
    )
    blocked = await blocked_job_clips(db, job_id)
    for event in candidates:
        if event.clip_id in blocked:
            event.provenance = {
                **(event.provenance or {}),
                "exclusion_reason": blocked[event.clip_id],
            }
    clips = {
        event.clip_id
        for event in candidates
        if event.clip_id not in blocked
        and (event.provenance or {}).get("publication_state") == "pending"
        and (event.provenance or {}).get("source") in {"real", "manual", "human"}
    }
    prior = (
        (await db.execute(select(Event).where(Event.clip_id.in_(clips)))).scalars().all()
        if clips
        else []
    )
    for event in prior:
        provenance = dict(event.provenance or {})
        if provenance.get("job_id") == str(job_id):
            provenance.pop("publication_state", None)
            provenance["result_version"] = str(job_id)
        elif (
            provenance.get("job_id")
            and provenance.get("source") not in {"manual", "human"}
            and (event.attributes or {}).get("source") not in {"manual", "human"}
        ):
            provenance["state"] = "superseded"
        event.provenance = provenance
    return len(candidates)


async def publish_job_metrics(db: AsyncSession, job_id: uuid.UUID) -> int:
    candidates = (await db.execute(select(Metric).where(Metric.job_id == job_id))).scalars().all()
    blocked_clips = await blocked_job_clips(db, job_id)
    promoted = 0
    replaced: set[tuple[uuid.UUID, uuid.UUID | None]] = set()
    for metric in candidates:
        provenance = dict(metric.provenance or {})
        if provenance.get("publication_state") != "pending":
            continue
        if metric.clip_id in blocked_clips:
            metric.provenance = {
                **provenance,
                "exclusion_reason": blocked_clips[metric.clip_id],
            }
            continue
        provenance.pop("publication_state", None)
        provenance["result_version"] = str(job_id)
        metric.provenance = provenance
        if exclusion_reason(metric) is not None:
            continue
        player_id = None
        if metric.tracklet_id:
            track = await db.get(Tracklet, metric.tracklet_id)
            player_id = track.player_id if track else None
        # Unknown identities cannot safely replace another athlete's measurement.
        if metric.tracklet_id and player_id is None:
            continue
        key = (metric.clip_id, player_id)
        if key in replaced:
            continue
        replaced.add(key)
        # Replace the complete automatic measurement set for the rep. Keeping
        # an old metric merely because its name is absent in the new result
        # would mix versions. Staff corrections remain independent overrides.
        query = select(Metric).where(
            Metric.clip_id == metric.clip_id,
            Metric.job_id.is_distinct_from(job_id),
            Metric.is_suppressed.is_(False),
        )
        if player_id:
            query = query.join(Tracklet, Tracklet.id == Metric.tracklet_id).where(
                Tracklet.player_id == player_id
            )
        else:
            query = query.where(Metric.tracklet_id.is_(None))
        for prior in (await db.execute(query)).scalars().all():
            if (prior.provenance or {}).get("source") in {"manual", "human"}:
                continue
            prior.is_suppressed = True
            prior.analytics_safe = False
            prior.suppression_reason = "newer_analysis_available"
            prior.provenance = {**(prior.provenance or {}), "state": "superseded"}
        promoted += 1
    return promoted


async def publish_job_tracks(db: AsyncSession, job_id: uuid.UUID) -> int:
    """Swap complete real tracking runs without exposing pending duplicates."""
    candidates = (
        (
            await db.execute(
                select(Tracklet)
                .where(Tracklet.job_id == job_id)
                .options(selectinload(Tracklet.track_points))
            )
        )
        .scalars()
        .all()
    )
    job = await db.get(ProcessingJob, job_id)
    touched = {track.clip_id for track in candidates}
    touched.update(
        (await db.execute(select(Metric.clip_id).where(Metric.job_id == job_id))).scalars().all()
    )
    touched.update(
        (
            await db.execute(
                select(Event.clip_id).where(Event.provenance["job_id"].astext == str(job_id))
            )
        )
        .scalars()
        .all()
    )
    if job and job.job_type == JobType.pipeline and job.video_id:
        touched.update(
            (await db.execute(select(Clip.id).where(Clip.video_id == job.video_id, active_clip())))
            .scalars()
            .all()
        )
    if not touched:
        return 0
    prior = (
        (
            await db.execute(
                select(Tracklet)
                .where(
                    Tracklet.clip_id.in_(touched),
                    Tracklet.job_id.is_distinct_from(job_id),
                    func.coalesce(Tracklet.provenance["state"].astext, "current") != "superseded",
                    func.coalesce(Tracklet.provenance["publication_state"].astext, "current")
                    != "pending",
                )
                .options(selectinload(Tracklet.track_points))
            )
        )
        .scalars()
        .all()
    )
    labels = (
        (
            await db.execute(
                select(Label)
                .where(
                    Label.tracklet_id.in_([track.id for track in prior]),
                    Label.label_type == "player_identity",
                    Label.source.in_(("human", "manual", "inherited_human")),
                )
                .order_by(Label.created_at)
            )
        )
        .scalars()
        .all()
        if prior
        else []
    )
    by_track = {label.tracklet_id: label for label in labels}
    candidates_by_clip: dict[uuid.UUID, list[Tracklet]] = defaultdict(list)
    for track in candidates:
        candidates_by_clip[track.clip_id].append(track)
    blocked: dict[uuid.UUID, str] = {}
    bindings: list[tuple[Tracklet, Tracklet, Label]] = []
    for old in prior:
        label = by_track.get(old.id)
        if label is None:
            continue
        fingerprint = observation_fingerprint(old)
        matches = [
            track
            for track in candidates_by_clip[old.clip_id]
            if fingerprint is not None and observation_fingerprint(track) == fingerprint
        ]
        if len(matches) != 1 or any(new.id == matches[0].id for _, new, _ in bindings):
            blocked[old.clip_id] = "correction_rebinding_unresolved"
        else:
            bindings.append((old, matches[0], label))
    for old, new, label in bindings:
        if old.clip_id in blocked:
            continue
        new.player_id, new.team_label = old.player_id, old.team_label
        new.provenance = {
            **(new.provenance or {}),
            "identity_override": {
                "source_tracklet_id": str(old.id),
                "source_label_id": str(label.id),
                "method": "identical_source_observations_v1",
            },
        }
        # Inherited authority is distinct from a fresh human annotation and
        # remains excluded from human-only training/holdout materialization.
        inherited = (
            await db.execute(
                select(Label)
                .where(
                    Label.tracklet_id == new.id,
                    Label.source == "inherited_human",
                    Label.label_value["inherited_from"].astext == str(label.id),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if inherited is None:
            db.add(
                Label(
                    id=uuid.uuid4(),
                    clip_id=new.clip_id,
                    tracklet_id=new.id,
                    label_type="player_identity",
                    source="inherited_human",
                    label_value={**label.label_value, "inherited_from": str(label.id)},
                    annotated_by=label.annotated_by,
                )
            )
        else:
            inherited.label_value = {**label.label_value, "inherited_from": str(label.id)}
            inherited.annotated_by = label.annotated_by
    for clip_id in await unqualified_replacement_clips(db, job_id, prior, candidates, touched):
        blocked.setdefault(clip_id, "replacement_not_qualified")
    if job:
        job.output_artifacts = {
            **(job.output_artifacts or {}),
            "publication_holds": {str(clip_id): reason for clip_id, reason in blocked.items()},
        }
    clips: set[uuid.UUID] = set()
    promoted = 0
    for track in candidates:
        provenance = dict(track.provenance or {})
        if provenance.get("publication_state") != "pending":
            continue
        if track.clip_id in blocked:
            track.provenance = {**provenance, "exclusion_reason": blocked[track.clip_id]}
            continue
        if provenance.get("source") not in {"real", "manual", "human"}:
            continue
        provenance.pop("publication_state", None)
        provenance["result_version"] = str(job_id)
        track.provenance = provenance
        clips.add(track.clip_id)
        promoted += 1
    for track in prior:
        if track.clip_id in clips:
            track.provenance = {**(track.provenance or {}), "state": "superseded"}
    return promoted


def observation_fingerprint(track: Tracklet) -> str | None:
    points = [
        (
            point.frame_number,
            point.timestamp_seconds,
            point.bbox,
            point.shot_id,
            point.visibility_state,
        )
        for point in track.track_points
        if point.bbox is not None
    ]
    if not points:
        return None
    ordered = sorted(points, key=lambda point: (point[0], point[1] or 0))
    return hashlib.sha256(json.dumps(ordered, separators=(",", ":")).encode()).hexdigest()
