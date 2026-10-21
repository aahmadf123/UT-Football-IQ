"""Declared roster cohorts only; these mappings never assign on-field roles."""

from typing import Any

from sqlalchemy import and_, func, or_

from app.models import Player

POSITION_ALIASES = {
    "QB": {"QB"},
    "RB": {"RB", "FB", "HB"},
    "WR": {"WR", "SLOT"},
    "TE": {"TE"},
    "OL": {"OL", "OT", "OG", "C", "G", "T", "LT", "LG", "RG", "RT"},
    "DL": {"DL", "DE", "DT", "NT"},
    "EDGE": {"EDGE"},
    "LB": {"LB", "MLB", "ILB", "OLB"},
    "DB": {"DB", "CB", "S", "SAF", "FS", "SS"},
    "ST": {"ST", "K", "PK", "P", "LS", "KR", "PR", "RET"},
}
POSITION_GROUPS = {group.lower(): group for group in (*POSITION_ALIASES, "Skill")}
POSITION_TO_GROUP = {
    position: group for group, positions in POSITION_ALIASES.items() for position in positions
}


def roster_cohort_condition(position: str) -> Any:
    requested = position.strip().upper()
    listed = func.upper(func.trim(Player.position))
    declared = func.upper(func.trim(Player.position_group))
    if requested == "SKILL":
        return declared == "SKILL"
    if requested not in POSITION_ALIASES:
        return listed == requested
    legacy = or_(Player.position_group.is_(None), declared == "")
    if requested in {"RB", "WR", "TE"}:
        legacy = or_(legacy, declared == "SKILL")
    return or_(declared == requested, and_(legacy, listed.in_(sorted(POSITION_ALIASES[requested]))))
