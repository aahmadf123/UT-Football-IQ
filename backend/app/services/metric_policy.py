"""One visibility policy for standalone, clip and overlay metric endpoints."""

from typing import Any

from sqlalchemy import Select, String, case, cast, func, or_, select

from app.models import Clip, Label, Metric, PlayView, Tracklet, User, UserRole
from app.services.clip_state import active_clip

HEALTH_WORKLOAD_ONLY_METRIC_NAMES = frozenset({"workload_fusion"})
INVALID_SOURCES = frozenset(
    {"stub", "synthetic", "synthetic_test", "unavailable", "disabled", "unverified_adapter"}
)


def publication_conditions() -> tuple[Any, ...]:
    """Shared SQL contract for automatically qualified and staff-entered values."""
    source = func.coalesce(
        Metric.provenance["source"].astext,
        Metric.metric_value["provenance"]["source"].astext,
        "unknown",
    )
    return (
        select(Clip.id).where(Clip.id == Metric.clip_id, active_clip()).correlate(Metric).exists(),
        Metric.is_suppressed.is_(False),
        Metric.analytics_safe.is_(True),
        ~Metric.metric_name.in_(HEALTH_WORKLOAD_ONLY_METRIC_NAMES),
        source.in_({"real", "manual", "human"}),
        func.coalesce(Metric.provenance["state"].astext, "current") != "superseded",
        func.coalesce(Metric.provenance["publication_state"].astext, "current") != "pending",
        func.coalesce(Metric.provenance["eligibility"].astext, "eligible") != "excluded",
    )


def metric_visibility(query: Select[Any], user: User) -> Select[Any]:
    query = query.where(*publication_conditions())
    if user.role in (UserRole.player, UserRole.viewer):
        query = query.where(Metric.experimental_flag.is_(False), Metric.analytics_safe.is_(True))
    return query


def exclusion_reason(metric: Metric) -> str | None:
    value = metric.metric_value if isinstance(metric.metric_value, dict) else {}
    provenance = (
        metric.provenance if isinstance(metric.provenance, dict) else value.get("provenance", {})
    )
    source = provenance.get("source") if isinstance(provenance, dict) else None
    if metric.is_suppressed or metric.metric_name in HEALTH_WORKLOAD_ONLY_METRIC_NAMES:
        return metric.suppression_reason or "suppressed"
    if provenance.get("state") == "superseded":
        return "superseded"
    if provenance.get("publication_state") == "pending":
        return str(provenance.get("exclusion_reason") or "analysis_in_progress")
    if provenance.get("eligibility") == "excluded":
        return str(provenance.get("exclusion_reason") or "quality_gate_failed")
    if source not in {"real", "manual", "human"} or not metric.analytics_safe:
        return (
            "unqualified_measurement" if source in {"real", "manual", "human"} else "unknown_source"
        )
    return None


def metric_visible(metric: Metric, user: User) -> bool:
    if exclusion_reason(metric) is not None:
        return False
    return not (
        user.role in (UserRole.player, UserRole.viewer)
        and (metric.experimental_flag or not metric.analytics_safe)
    )


def canonical_play_id() -> Any:
    """Scalar expression usable without multiplying rows through angle joins."""
    matched = select(PlayView.play_id).where(PlayView.clip_id == Clip.id).scalar_subquery()
    return func.coalesce(matched, Clip.id)


def canonical_play_eligible() -> Any:
    return active_clip() & func.coalesce(
        Clip.label_data["canonical_count_eligible"].as_boolean(), True
    ).is_(True)


def selected_trajectory_ids() -> Any:
    """Select a source once per athlete-play, independent of metric choice."""
    human_override = (
        select(Label.id)
        .where(
            Label.tracklet_id == Tracklet.id,
            Label.label_type == "player_identity",
            Label.source.in_(("human", "manual", "inherited_human")),
        )
        .exists()
    )
    ranked = (
        select(
            Tracklet.id.label("id"),
            func.row_number()
            .over(
                partition_by=(canonical_play_id(), Tracklet.player_id),
                order_by=(
                    human_override.desc(),
                    (Tracklet.end_frame - Tracklet.start_frame).desc(),
                    Tracklet.track_confidence.desc().nullslast(),
                    Tracklet.created_at.desc(),
                    Tracklet.id,
                ),
            )
            .label("rank"),
        )
        .join(Clip, Clip.id == Tracklet.clip_id)
        .where(
            Tracklet.player_id.is_not(None),
            canonical_play_eligible(),
            func.coalesce(Tracklet.provenance["state"].astext, "current") != "superseded",
            func.coalesce(Tracklet.provenance["publication_state"].astext, "current") != "pending",
        )
        .subquery()
    )
    return select(ranked.c.id).where(ranked.c.rank == 1)


def aggregate_metric_ids() -> Any:
    """One qualified trajectory/measurement per resolved athlete and play.

    Unknown identity cannot be deduplicated across fragments or recordings, so
    its measurements remain evidence only rather than inflating team totals.
    """
    rank = func.row_number().over(
        partition_by=(
            canonical_play_id(),
            func.coalesce(cast(Tracklet.player_id, String), "team"),
            Metric.metric_name,
            func.coalesce(
                Metric.metric_value["subject_id"].astext,
                case(
                    (
                        Metric.metric_value["gap_index"].astext.is_not(None),
                        func.concat("gap:", Metric.metric_value["gap_index"].astext),
                    )
                ),
                "team",
            ),
        ),
        order_by=(
            case((Metric.provenance["source"].astext.in_(("manual", "human")), 1), else_=0).desc(),
            Metric.confidence.desc().nullslast(),
            Metric.created_at.desc(),
            Metric.id,
        ),
    )
    ranked = (
        select(Metric.id.label("metric_id"), rank.label("rank"))
        .join(Clip, Clip.id == Metric.clip_id)
        .outerjoin(Tracklet, Tracklet.id == Metric.tracklet_id)
        .where(
            *publication_conditions(),
            canonical_play_eligible(),
            Metric.experimental_flag.is_(False),
            or_(Metric.tracklet_id.is_(None), Tracklet.player_id.is_not(None)),
            or_(
                Metric.tracklet_id.is_(None),
                Tracklet.id.in_(selected_trajectory_ids()),
                Metric.provenance["source"].astext.in_(("manual", "human")),
            ),
        )
    ).subquery()
    return select(ranked.c.metric_id).where(ranked.c.rank == 1)
