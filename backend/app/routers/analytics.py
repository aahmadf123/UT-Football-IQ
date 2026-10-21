"""Cohort analytics with canonical denominators and direct film evidence."""

import hashlib
import json
import math
import uuid
from collections import Counter, defaultdict
from datetime import datetime
from statistics import mean
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import and_, extract, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import require_any_staff
from app.models import (
    Clip,
    FootballSession,
    Metric,
    Player,
    SessionKind,
    SideOfBall,
    Tracklet,
    User,
    Video,
)
from app.services.clip_state import active_clip, clip_is_active
from app.services.metric_definitions import DEFINITION_VERSION, DEFINITIONS
from app.services.metric_policy import canonical_play_id, exclusion_reason, selected_trajectory_ids

router = APIRouter(prefix="/api/v1/analytics", tags=["analytics"])


class AnalyticsQuery(BaseModel):
    model_config = {"extra": "forbid"}
    metric_names: list[str] = Field(default_factory=list, max_length=50)
    session_kind: SessionKind | None = None
    session_ids: list[uuid.UUID] = Field(default_factory=list, max_length=200)
    library_role: Literal["toledo", "opponent"] | None = None
    side_of_ball: SideOfBall | None = None
    season: int | None = Field(default=None, ge=1900, le=2200)
    player_id: uuid.UUID | None = None
    position_group: str | None = Field(default=None, max_length=20)
    position: str | None = Field(default=None, max_length=50)
    personnel: str | None = Field(default=None, max_length=20)
    formation: str | None = Field(default=None, max_length=100)
    situation: str | None = Field(default=None, max_length=100)
    opponent: str | None = Field(default=None, max_length=120)
    since: datetime | None = None
    until: datetime | None = None
    group_by: Literal["metric", "player", "position", "session_kind"] = "metric"
    minimum_sample: int = Field(default=5, ge=1, le=1000)
    drill_type: str | None = Field(default=None, max_length=100)
    contact_level: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def dates(self) -> "AnalyticsQuery":
        if self.since and self.until:
            try:
                if self.since > self.until:
                    raise ValueError("since must be before until")
            except TypeError as exc:
                raise ValueError("Date filters must use matching timezones") from exc
        return self


def quantile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    index = (len(ordered) - 1) * probability
    low = math.floor(index)
    high = math.ceil(index)
    return ordered[low] + (ordered[high] - ordered[low]) * (index - low)


def survival_curve(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Kaplan-Meier steps; censored observations leave the risk set, not zeros."""
    at_risk = len(evidence)
    survival = 1.0
    steps = [{"seconds": 0.0, "at_risk": at_risk, "events": 0, "censored": 0, "survival": survival}]
    for seconds in sorted({row["value"] for row in evidence}):
        same_time = [row for row in evidence if row["value"] == seconds]
        events = sum(not row["right_censored"] for row in same_time)
        censored = len(same_time) - events
        survival *= 1.0 - events / at_risk
        steps.append(
            {
                "seconds": seconds,
                "at_risk": at_risk,
                "events": events,
                "censored": censored,
                "survival": survival,
            }
        )
        at_risk -= len(same_time)
    return steps


def scalar_value(metric: Metric) -> float | None:
    packet = metric.metric_value or {}
    definition = DEFINITIONS.get(metric.metric_name, {})
    # Composite packets require an explicitly registered scalar definition.
    keys = [definition["scalar_key"]] if definition else ["value"]
    for key in keys:
        value = packet.get(key) if key else None
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            return float(value)
    return None


def metric_subject(metric: Metric) -> str:
    value = metric.metric_value or {}
    if value.get("subject_id") is not None:
        return str(value["subject_id"])
    if value.get("gap_index") is not None:
        return f"gap:{value['gap_index']}"
    return "team"


def summarize_rows(
    rows: list[Any], body: AnalyticsQuery, trajectory_ids: set[uuid.UUID] | None = None
) -> dict[str, Any]:
    exclusions: Counter[str] = Counter()
    chosen: dict[tuple[str, str, str, str, str, str], tuple[Any, float]] = {}
    plays: set[str] = set()
    participants: set[tuple[str, str]] = set()
    for row in rows:
        metric, clip, _video, track, _player, play_id = row
        canonical = str(play_id)
        if not clip_is_active(clip):
            exclusions["superseded_clip"] += 1
            continue
        if (clip.label_data or {}).get("canonical_count_eligible") is False:
            exclusions["unresolved_logical_play"] += 1
            continue
        plays.add(canonical)
        if metric is None:
            exclusions["no_measurement"] += 1
            continue
        reason = exclusion_reason(metric)
        if not reason and metric.experimental_flag:
            reason = "experimental"
        if not reason and metric.tracklet_id and (track is None or track.player_id is None):
            reason = "unresolved_player"
        if (
            not reason
            and metric.tracklet_id
            and trajectory_ids is not None
            and metric.tracklet_id not in trajectory_ids
            and (metric.provenance or {}).get("source") not in {"manual", "human"}
        ):
            reason = "unselected_trajectory"
        value = scalar_value(metric)
        if (
            not reason
            and metric.metric_value.get("right_censored") is True
            and metric.metric_name
            not in {"ball_holding_duration", "time_to_first_pressure", "tackle_box_residence"}
        ):
            reason = "invalid_censoring_metric"
        if not reason and value is None:
            reason = "no_scalar_observation"
        if reason:
            exclusions[reason] += 1
            continue
        provenance = metric.provenance or {}
        version = str(
            provenance.get("definition_version")
            or metric.metric_value.get("definition_version")
            or "legacy_unversioned"
        )
        athlete = str(track.player_id) if track and track.player_id else "team"
        key = (
            canonical,
            athlete,
            metric.metric_name,
            version,
            metric_subject(metric),
            metric.unit or "",
        )
        previous = chosen.get(key)
        # Human corrections win; otherwise pick the strongest qualified view,
        # then the latest complete result. Fragments never add extra reps.
        preference = (
            provenance.get("source") in {"manual", "human"},
            metric.confidence if metric.confidence is not None else -1,
            metric.created_at.isoformat(),
            str(metric.id),
        )
        if previous:
            old = previous[0][0]
            old_preference = (
                (old.provenance or {}).get("source") in {"manual", "human"},
                old.confidence if old.confidence is not None else -1,
                old.created_at.isoformat(),
                str(old.id),
            )
            exclusions["alternate_view_or_fragment"] += 1
            if preference <= old_preference:
                continue
        assert value is not None
        chosen[key] = (row, value)

    grouped: dict[tuple[str, str, str, str | None], list[dict[str, Any]]] = defaultdict(list)
    digest = hashlib.sha256(
        json.dumps(
            {
                "query": body.model_dump(mode="json"),
                "records": sorted(
                    [
                        {
                            "clip": str(row[1].id),
                            "play": str(row[5]),
                            "metric": str(row[0].id) if row[0] else None,
                            "value": row[0].metric_value if row[0] else None,
                            "provenance": row[0].provenance if row[0] else None,
                            "eligible": exclusion_reason(row[0]) if row[0] else "no_measurement",
                            "player": str(row[3].player_id)
                            if row[3] and row[3].player_id
                            else None,
                        }
                        for row in rows
                    ],
                    key=lambda record: (record["clip"], record["metric"] or ""),
                ),
            },
            sort_keys=True,
            default=str,
        ).encode()
    )
    for key, (row, value) in sorted(chosen.items()):
        metric, clip, video, track, player, play_id = row
        canonical, athlete, name, version, subject, _unit = key
        if athlete != "team":
            participants.add((canonical, athlete))
        group = name
        if body.group_by == "player":
            group = athlete
        elif body.group_by == "position":
            group = (
                (player.position if player else None)
                or (track.position_group if track else None)
                or "unknown"
            )
        elif body.group_by == "session_kind":
            group = str(clip.session_kind or video.session_kind or "unknown")
        if subject != "team":
            group = f"{group} · {subject}"
        digest.update(f"{metric.id}:{version}:{metric.created_at.isoformat()}".encode())
        grouped[(group, name, version, metric.unit)].append(
            {
                "play_id": str(play_id),
                "clip_id": str(clip.id),
                "metric_id": str(metric.id),
                "video_id": str(video.id),
                "player_id": athlete if athlete != "team" else None,
                "value": value,
                "start_seconds": clip.start_time,
                "end_seconds": clip.end_time,
                "source_timestamps": (metric.provenance or {}).get("source_timestamps"),
                "right_censored": metric.metric_value.get("right_censored") is True,
                "endpoint": metric.metric_value.get("endpoint"),
                "subject": subject,
                "dimensions": {
                    key: metric.metric_value[key]
                    for key in ("gap_index", "initial_pair")
                    if key in metric.metric_value
                },
                "tracklet_id": str(metric.tracklet_id) if metric.tracklet_id else None,
                "metric_packet": metric.metric_value,
                "result_version": (metric.provenance or {}).get("result_version")
                or (str(metric.job_id) if metric.job_id else None),
            }
        )
    groups = []
    censoring = []
    censored_measurements = 0
    for (group, name, version, unit), evidence in sorted(
        grouped.items(), key=lambda item: str(item[0])
    ):
        censored = sum(item["right_censored"] for item in evidence)
        censored_measurements += censored
        if censored:
            censoring.append(
                {
                    "key": group,
                    "metric_name": name,
                    "definition_version": version,
                    "n": len(evidence),
                    "events": len(evidence) - censored,
                    "censored": censored,
                    "unit": unit,
                    "evidence": evidence,
                    "survival_curve": survival_curve(evidence),
                }
            )
        completed = [item for item in evidence if not item["right_censored"]]
        if not completed:
            continue
        values = [item["value"] for item in completed]
        groups.append(
            {
                "key": group,
                "metric_name": name,
                "definition_version": version,
                "n": len(values),
                "eligible_count": len(values),
                "censored_count": censored,
                "mean": mean(values),
                "median": quantile(values, 0.5),
                "p10": quantile(values, 0.1),
                "p90": quantile(values, 0.9),
                "min": min(values),
                "max": max(values),
                "unit": unit,
                "low_sample": len(values) < body.minimum_sample,
                "evidence": completed,
            }
        )
    return {
        "dataset_version": digest.hexdigest(),
        "definition_version": DEFINITION_VERSION,
        "cohort": {
            "plays": len(plays),
            "player_reps": len(participants),
            "eligible_measurements": len(chosen),
            "censored_measurements": censored_measurements,
            "excluded_measurements": sum(exclusions.values()),
            "unknown_count": sum(
                count
                for reason, count in exclusions.items()
                if reason
                not in {"alternate_view_or_fragment", "unselected_trajectory", "superseded"}
            ),
        },
        "groups": groups,
        "censoring": censoring,
        "exclusions": [
            {"reason": reason, "count": count} for reason, count in sorted(exclusions.items())
        ],
    }


@router.post("/query")
async def query_analytics(
    body: AnalyticsQuery,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    _user: Annotated[User, Depends(require_any_staff)],
) -> dict[str, Any]:
    recorded = func.coalesce(Video.recorded_at, Video.created_at)
    metric_join = [Metric.clip_id == Clip.id, Metric.metric_name != "workload_fusion"]
    if body.metric_names:
        metric_join.append(Metric.metric_name.in_(body.metric_names))
    stmt = (
        select(Metric, Clip, Video, Tracklet, Player, canonical_play_id().label("play_id"))
        .select_from(Clip)
        .outerjoin(Metric, and_(*metric_join))
        .join(Video, Video.id == Clip.video_id)
        .outerjoin(Tracklet, Tracklet.id == Metric.tracklet_id)
        .outerjoin(Player, Player.id == Tracklet.player_id)
        .outerjoin(FootballSession, FootballSession.id == Video.practice_session_id)
        .where(active_clip())
    )
    if body.session_kind:
        stmt = stmt.where(func.coalesce(Clip.session_kind, Video.session_kind) == body.session_kind)
    if body.session_ids:
        stmt = stmt.where(Video.practice_session_id.in_(body.session_ids))
    if body.library_role:
        stmt = stmt.where(Video.library_role == body.library_role)
    if body.side_of_ball:
        stmt = stmt.where(Clip.side_of_ball == body.side_of_ball)
    if body.season:
        stmt = stmt.where(extract("year", recorded) == body.season)
    if body.player_id:
        stmt = stmt.where(Tracklet.player_id == body.player_id)
    if body.position_group:
        stmt = stmt.where(func.upper(Player.position_group) == body.position_group.upper())
    if body.position:
        stmt = stmt.where(func.upper(Player.position) == body.position.upper())
    if body.personnel:
        stmt = stmt.where(Clip.personnel_grouping == body.personnel)
    if body.formation:
        stmt = stmt.where(
            or_(
                Clip.label_data["formation"].astext == body.formation,
                Clip.label_data["offensive_formation"].astext == body.formation,
            )
        )
    if body.situation:
        stmt = stmt.where(
            or_(
                Clip.field_zone == body.situation,
                Clip.label_data["situation"].astext == body.situation,
            )
        )
    if body.opponent:
        stmt = stmt.where(func.lower(Video.opponent_team) == body.opponent.lower())
    if body.since:
        stmt = stmt.where(recorded >= body.since)
    if body.until:
        stmt = stmt.where(recorded <= body.until)
    if body.drill_type:
        stmt = stmt.where(Video.metadata_["drill_type"].astext == body.drill_type)
    if body.contact_level:
        stmt = stmt.where(Video.metadata_["contact_level"].astext == body.contact_level)
    rows = list((await db.execute(stmt.limit(20001))).all())
    if len(rows) > 20000:
        raise HTTPException(413, "Narrow the cohort to at most 20,000 measurements")
    trajectory_ids = set((await db.execute(selected_trajectory_ids())).scalars().all())
    return summarize_rows(rows, body, trajectory_ids)
