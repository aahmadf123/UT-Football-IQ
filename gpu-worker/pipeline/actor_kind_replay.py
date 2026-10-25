"""Replay staff person-kind decisions through exact source observations only."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from pipeline.actor_bindings import rebind_reviewed_events

KINDS = {"player", "official", "sideline", "unknown"}


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _created(value: Any) -> datetime | None:
    try:
        result = (
            value
            if isinstance(value, datetime)
            else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        )
        return result.astimezone(UTC) if result.tzinfo else None
    except (ValueError, TypeError, OverflowError):
        return None


def _uuid(value: Any) -> bool:
    try:
        UUID(str(value))
        return True
    except (ValueError, TypeError, AttributeError):
        return False


def _binding_actor(label: dict[str, Any]) -> str | None:
    value = _mapping(label.get("label_value"))
    if label.get("source") != "inherited_human":
        return str(label["tracklet_id"]) if label.get("tracklet_id") else None
    references = _mapping(_mapping(value.get("actor_bindings")).get("references"))
    if (
        len(references) != 1
        or not _uuid(value.get("actor_review_origin_label_id"))
        or not _uuid(value.get("inherited_from"))
    ):
        return None
    actor = next(iter(references))
    return actor if isinstance(actor, str) and actor else None


def _kind(track: dict[str, Any]) -> str:
    provenance = _mapping(track.get("provenance"))
    for candidate in (provenance.get("actor_kind"), track.get("class")):
        if isinstance(candidate, str) and candidate in KINDS:
            return candidate
    return track["team_label"] if track.get("team_label") in {"official", "sideline"} else "player"


def _clear_identity(track: dict[str, Any]) -> None:
    track.update(
        player_id=None, identity_state="unresolved", identity_verified=False, position_group=None
    )
    track["provenance"] = {
        key: value
        for key, value in _mapping(track.get("provenance")).items()
        if key not in {"identity", "manual_identity_binding"}
    }


def _set_identity(track: dict[str, Any], claim: dict[str, Any]) -> None:
    track.update(player_id=claim["player_id"], identity_state="resolved", identity_verified=True)
    track["provenance"] = {
        **_mapping(track.get("provenance")),
        "identity": {"source": "manual", "label_id": claim["label_id"]},
        "manual_identity_binding": deepcopy(claim),
    }


def _bound_identity(track: dict[str, Any]) -> dict[str, Any] | None:
    claim = _mapping(_mapping(track.get("provenance")).get("manual_identity_binding"))
    if (
        claim.get("source") in ("human", "manual")
        and _uuid(claim.get("label_id"))
        and _uuid(claim.get("annotated_by"))
        and _uuid(claim.get("player_id"))
        and claim.get("original_track_id")
        and claim.get("current_track_id") == track.get("id")
        and claim.get("player_id") == track.get("player_id")
    ):
        return deepcopy(claim)
    return None


def _classify(track: dict[str, Any], kind: str, proof: dict[str, Any]) -> None:
    previous = _kind(track)
    identity = _bound_identity(track) if kind == "player" else None
    track["class"] = kind
    provenance = _mapping(track.get("provenance"))
    if kind != "player" or previous != "player":
        # A person-kind decision supplies no jersey, roster identity or unit.
        track.update(
            player_id=None,
            identity_state="unresolved",
            identity_verified=False,
            position_group=None,
            side_of_ball=None,
            observed_role=None,
            role_resolved=False,
        )
        track["team_label"] = kind if kind in {"official", "sideline"} else "unknown"
        provenance = {
            key: value
            for key, value in provenance.items()
            if key not in {"identity", "manual_identity_binding", "unit_source"}
        }
    track["provenance"] = {**provenance, "actor_kind": kind, "actor_classification": proof}
    if identity:
        _set_identity(track, identity)


def replay_actor_kinds(
    labels: list[dict[str, Any]],
    players: list[dict[str, Any]],
    nonplayers: list[dict[str, Any]],
    scope: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Return separate copied pools before any player analysis, with review issues.

    Invalid manual authority holds this clip's current actors outside player
    analysis: without an exact match we cannot know which current person the
    unresolved decision concerns. Prior published annotations are untouched.
    """
    tracks = deepcopy([*players, *nonplayers])
    grouped: dict[str, list[tuple[datetime, dict[str, Any]]]] = {}
    issues: list[dict[str, Any]] = []
    for label in labels:
        if (
            not isinstance(label, dict)
            or label.get("label_type") != "player_identity"
            or label.get("source") not in ("human", "manual", "inherited_human")
        ):
            continue
        value = _mapping(label.get("label_value"))
        if "actor_kind" not in value:
            continue
        when = _created(label.get("created_at"))
        actor = label.get("tracklet_id")
        if (
            not label.get("id")
            or not actor
            or not _binding_actor(label)
            or not label.get("annotated_by")
            or label.get("clip_id") != scope.get("clip_id")
            or when is None
            or not isinstance(value.get("actor_kind"), str)
            or value["actor_kind"] not in KINDS
        ):
            issues.append(
                {
                    "label_id": label.get("id"),
                    "actor_id": actor,
                    "reason": "actor_kind_label_incomplete_or_invalid",
                    "review_required": True,
                }
            )
            continue
        grouped.setdefault(str(actor), []).append((when, label))

    latest = []
    for actor, candidates in grouped.items():
        newest = max(when for when, _ in candidates)
        choices = {str(item["id"]): item for when, item in candidates if when == newest}
        if len(choices) != 1:
            issues.append(
                {
                    "actor_id": actor,
                    "reason": "actor_kind_latest_label_ambiguous",
                    "review_required": True,
                }
            )
            continue
        latest.append(next(iter(choices.values())))
    events = [
        {
            "id": entry["id"],
            "event_type": "actor_kind_review",
            "attributes": {"actor_id": _binding_actor(entry)},
            "provenance": {"actor_bindings": _mapping(entry["label_value"]).get("actor_bindings")},
        }
        for entry in latest
    ]
    rebound, tracks, binding_issues = rebind_reviewed_events(events, tracks, scope)
    issues.extend(
        {
            "label_id": issue.get("event_id"),
            "actor_ids": issue.get("actor_ids", []),
            "reason": issue["reason"],
            "review_required": True,
        }
        for issue in binding_issues
    )
    by_label = {str(entry["id"]): entry for entry in latest}
    by_track = {str(track.get("id")): track for track in tracks}
    for event in rebound:
        entry = by_label[str(event["id"])]
        target = by_track[event["attributes"]["actor_id"]]
        value = entry["label_value"]
        units = [value[key] for key in ("team", "team_label") if value.get(key) is not None]
        if (
            any(
                not isinstance(unit, str)
                or unit not in {"home", "away", "offense", "defense", "special_teams", "unknown"}
                for unit in units
            )
            or len(set(units)) > 1
        ):
            issues.append(
                {
                    "label_id": entry["id"],
                    "reason": "actor_kind_unit_labels_invalid_or_conflicting",
                    "review_required": True,
                }
            )
            continue
        _classify(
            target,
            entry["label_value"]["actor_kind"],
            {
                "method": "staff_review",
                "validated": True,
                "inherited_from": entry["label_value"].get("actor_review_origin_label_id")
                if entry.get("source") == "inherited_human"
                else entry["id"],
                "replayed_label_id": entry["id"],
                "reviewed_by": entry["annotated_by"],
                "actor_binding_lineage": event["provenance"]["actor_binding_lineage"],
            },
        )
        if value["actor_kind"] == "player":
            target["team_label"] = units[0] if units else "unknown"
            target["side_of_ball"] = (
                units[0] if units and units[0] in {"offense", "defense", "special_teams"} else None
            )
            target["provenance"].pop("unit_source", None)
            if units:
                target["provenance"]["unit_source"] = "staff_review"
            if entry.get("source") in {"human", "manual"} and "player_id" in value:
                if value["player_id"] is None:
                    _clear_identity(target)
                elif not all(
                    _uuid(item) for item in (value["player_id"], entry["id"], entry["annotated_by"])
                ):
                    issues.append(
                        {
                            "label_id": entry["id"],
                            "reason": "actor_kind_manual_identity_invalid",
                            "review_required": True,
                        }
                    )
                else:
                    actor = _binding_actor(entry)
                    reference = _mapping(
                        _mapping(_mapping(value.get("actor_bindings")).get("references")).get(actor)
                    )
                    _set_identity(
                        target,
                        {
                            "source": entry["source"],
                            "label_id": entry["id"],
                            "annotated_by": entry["annotated_by"],
                            "player_id": value["player_id"],
                            "original_track_id": actor,
                            "current_track_id": target["id"],
                            "observation_sha256": reference.get("observation_sha256"),
                        },
                    )
    if issues:
        for track in tracks:
            _classify(track, "unknown", {"method": "review_required", "validated": False})
    return (
        [track for track in tracks if _kind(track) == "player"],
        [track for track in tracks if _kind(track) != "player"],
        issues,
    )
