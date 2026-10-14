"""One visibility policy for standalone, clip and overlay metric endpoints."""

from typing import Any

from sqlalchemy import Select, func, or_

from app.models import Metric, User, UserRole

HEALTH_WORKLOAD_ONLY_METRIC_NAMES = frozenset({"workload_fusion"})
INVALID_SOURCES = frozenset(
    {"stub", "synthetic", "synthetic_test", "unavailable", "disabled", "unverified_adapter"}
)


def metric_visibility(query: Select[Any], user: User) -> Select[Any]:
    source = func.coalesce(
        Metric.provenance["source"].astext,
        Metric.metric_value["provenance"]["source"].astext,
        "unknown",
    )
    query = query.where(
        Metric.is_suppressed.is_(False),
        Metric.analytics_safe.is_(True),
        ~Metric.metric_name.in_(HEALTH_WORKLOAD_ONLY_METRIC_NAMES),
        source.in_({"real", "manual", "human"}),
        func.coalesce(Metric.provenance["state"].astext, "current") != "superseded",
        or_(Metric.experimental_flag.is_(False), Metric.analytics_safe.is_(True)),
    )
    if user.role in (UserRole.player, UserRole.viewer):
        query = query.where(Metric.experimental_flag.is_(False), Metric.analytics_safe.is_(True))
    return query


def metric_visible(metric: Metric, user: User) -> bool:
    value = metric.metric_value if isinstance(metric.metric_value, dict) else {}
    provenance = (
        metric.provenance if isinstance(metric.provenance, dict) else value.get("provenance", {})
    )
    source = provenance.get("source") if isinstance(provenance, dict) else None
    if metric.is_suppressed or metric.metric_name in HEALTH_WORKLOAD_ONLY_METRIC_NAMES:
        return False
    if source not in {"real", "manual", "human"} or not metric.analytics_safe:
        return False
    if isinstance(provenance, dict) and provenance.get("state") == "superseded":
        return False
    return not (
        user.role in (UserRole.player, UserRole.viewer)
        and (metric.experimental_flag or not metric.analytics_safe)
    )
