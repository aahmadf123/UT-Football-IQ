"""Game breakdown from film linked to CFBD play-by-play.

Once a game film's clips carry their official result (``label_data.cfbd``,
see :mod:`app.cfbd.game_link`), this rolls them up the way a coach reads a
game: Toledo's offense and defense, run vs pass, success rate, third downs,
explosive plays, turnovers and EPA -- and every number keeps the clips behind
it, so clicking "3rd down: 5 of 12" plays exactly those snaps.

Definitions (standard college analytics):
    * success: >= 50% of the distance on 1st down, >= 70% on 2nd, all of it
      on 3rd and 4th
    * explosive: a run of 10+ yards or a pass of 20+ yards
    * EPA: CFBD's predicted points added (``ppa``)
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cfbd.game_link import TOLEDO
from app.database import get_db
from app.deps import require_any_staff
from app.models import Clip, User, Video
from app.services.clip_state import active_clip

router = APIRouter(prefix="/api/v1/videos", tags=["game-summary"])

RUSH_TYPES = {"Rush", "Rushing Touchdown"}
PASS_TYPES = {
    "Pass",
    "Pass Reception",
    "Pass Completion",
    "Pass Incompletion",
    "Passing Touchdown",
    "Sack",
    "Pass Interception",
    "Pass Interception Return",
    "Interception Return Touchdown",
}
FUMBLE_TYPES = {"Fumble Recovery (Own)", "Fumble Recovery (Opponent)", "Fumble Return Touchdown"}
TURNOVER_TYPES = {
    "Pass Interception",
    "Pass Interception Return",
    "Interception Return Touchdown",
    "Fumble Recovery (Opponent)",
    "Fumble Return Touchdown",
}
EXPLOSIVE_RUSH_YARDS = 10
EXPLOSIVE_PASS_YARDS = 20


class PlayGroup(BaseModel):
    """A clickable slice of plays: its label, count and one clip per play."""

    key: str
    label: str
    plays: int
    clip_ids: list[uuid.UUID]


class DownSummary(BaseModel):
    down: int
    plays: int
    success_rate: float | None
    yards_per_play: float | None
    rush_share: float | None
    clip_ids: list[uuid.UUID]


class PlayDetail(BaseModel):
    """One counted play, in game order, for play-by-play charts."""

    clip_id: uuid.UUID
    #: The film's play number: one order across both sides of the ball.
    play_number: int | None = None
    period: int | None
    down: int | None
    distance: int | None
    yards_gained: int
    #: ``rush``, ``pass`` or ``other`` (a fumble that can't be classified).
    kind: str
    epa: float | None
    success: bool | None
    explosive: bool
    turnover: bool
    scoring: bool
    text: str | None


class SideSummary(BaseModel):
    plays: int
    yards: int
    yards_per_play: float | None
    success_rate: float | None
    epa_per_play: float | None
    rush_plays: int
    rush_yards: int
    rush_yards_per_play: float | None
    rush_success_rate: float | None
    pass_plays: int
    pass_yards: int
    pass_yards_per_play: float | None
    pass_success_rate: float | None
    third_down_attempts: int
    third_down_conversions: int
    explosive_plays: int
    turnovers: int
    by_down: list[DownSummary]
    groups: list[PlayGroup]
    play_log: list[PlayDetail] = []


class GameSummary(BaseModel):
    video_id: uuid.UUID
    opponent: str | None
    film_plays: int
    linked_plays: int
    offense: SideSummary | None
    defense: SideSummary | None


def play_kind(play: dict[str, Any]) -> str | None:
    """``rush``, ``pass`` or ``None`` (special teams, penalties, admin rows)."""
    play_type = play.get("play_type") or ""
    if play_type in RUSH_TYPES:
        return "rush"
    if play_type in PASS_TYPES:
        return "pass"
    if play_type in FUMBLE_TYPES:
        text = (play.get("play_text") or "").lower()
        if "pass" in text or "sack" in text:
            return "pass"
        if " run " in f" {text} " or "rush" in text:
            return "rush"
    return None


def is_success(play: dict[str, Any]) -> bool | None:
    down, distance, gained = play.get("down"), play.get("distance"), play.get("yards_gained")
    if not down or distance is None or gained is None:
        return None
    gained, distance = float(gained), float(distance)
    if distance <= 0:
        return gained > 0
    need = {1: 0.5, 2: 0.7}.get(int(down), 1.0) * distance
    return gained >= need


def is_explosive(play: dict[str, Any], kind: str) -> bool:
    gained = float(play.get("yards_gained") or 0)
    return gained >= (EXPLOSIVE_RUSH_YARDS if kind == "rush" else EXPLOSIVE_PASS_YARDS)


def _rate(hits: int, total: int) -> float | None:
    return round(hits / total, 3) if total else None


def _per(total: float, count: int) -> float | None:
    return round(total / count, 2) if count else None


def summarize_side(plays: list[tuple[dict[str, Any], uuid.UUID]]) -> SideSummary:
    """Roll up one side's scrimmage plays; each item is ``(cfbd, clip_id)``."""
    groups: dict[str, list[uuid.UUID]] = {
        key: []
        for key in (
            "all",
            "rush",
            "pass",
            "success",
            "explosive",
            "third_down",
            "third_down_converted",
            "turnovers",
            "negative",
            "scoring",
        )
    }
    yards = rush_yards = pass_yards = 0
    epa_total = 0.0
    epa_count = 0
    successes = {"all": [0, 0], "rush": [0, 0], "pass": [0, 0]}
    downs: dict[int, dict[str, Any]] = {}
    log: list[PlayDetail] = []
    for play, clip_id in plays:
        kind = play_kind(play)
        if kind is None and play.get("play_type") not in FUMBLE_TYPES:
            continue
        gained = int(play.get("yards_gained") or 0)
        groups["all"].append(clip_id)
        if kind is not None:
            groups[kind].append(clip_id)
        yards += gained
        if kind == "rush":
            rush_yards += gained
        elif kind == "pass":
            pass_yards += gained
        if play.get("ppa") is not None:
            epa_total += float(play["ppa"])
            epa_count += 1
        success = is_success(play)
        if success is not None:
            successes["all"][1] += 1
            successes["all"][0] += int(success)
            if kind is not None:
                successes[kind][1] += 1
                successes[kind][0] += int(success)
            if success:
                groups["success"].append(clip_id)
        if kind is not None and is_explosive(play, kind):
            groups["explosive"].append(clip_id)
        if play.get("play_type") in TURNOVER_TYPES:
            groups["turnovers"].append(clip_id)
        if gained < 0:
            groups["negative"].append(clip_id)
        if play.get("scoring"):
            groups["scoring"].append(clip_id)
        log.append(
            PlayDetail(
                clip_id=clip_id,
                play_number=play.get("_play_number"),
                period=play.get("period"),
                down=play.get("down") if play.get("down") in (1, 2, 3, 4) else None,
                distance=play.get("distance"),
                yards_gained=gained,
                kind=kind or "other",
                epa=round(float(play["ppa"]), 3) if play.get("ppa") is not None else None,
                success=success,
                explosive=kind is not None and is_explosive(play, kind),
                turnover=play.get("play_type") in TURNOVER_TYPES,
                scoring=bool(play.get("scoring")),
                text=play.get("play_text"),
            )
        )
        down = play.get("down")
        if down in (1, 2, 3, 4):
            row = downs.setdefault(
                int(down),
                {"plays": 0, "yards": 0, "rush": 0, "success": 0, "graded": 0, "clip_ids": []},
            )
            row["plays"] += 1
            row["yards"] += gained
            row["rush"] += int(kind == "rush")
            row["clip_ids"].append(clip_id)
            if success is not None:
                row["graded"] += 1
                row["success"] += int(success)
            if down == 3:
                groups["third_down"].append(clip_id)
                if (play.get("distance") is not None) and gained >= play["distance"]:
                    groups["third_down_converted"].append(clip_id)

    labels = {
        "all": "All plays",
        "rush": "Runs",
        "pass": "Passes",
        "success": "Successful plays",
        "explosive": "Explosive plays",
        "third_down": "3rd downs",
        "third_down_converted": "3rd-down conversions",
        "turnovers": "Turnovers",
        "negative": "Negative plays",
        "scoring": "Scoring plays",
    }
    total = len(groups["all"])
    return SideSummary(
        plays=total,
        yards=yards,
        yards_per_play=_per(yards, total),
        success_rate=_rate(*successes["all"]),
        epa_per_play=round(epa_total / epa_count, 3) if epa_count else None,
        rush_plays=len(groups["rush"]),
        rush_yards=rush_yards,
        rush_yards_per_play=_per(rush_yards, len(groups["rush"])),
        rush_success_rate=_rate(*successes["rush"]),
        pass_plays=len(groups["pass"]),
        pass_yards=pass_yards,
        pass_yards_per_play=_per(pass_yards, len(groups["pass"])),
        pass_success_rate=_rate(*successes["pass"]),
        third_down_attempts=len(groups["third_down"]),
        third_down_conversions=len(groups["third_down_converted"]),
        explosive_plays=len(groups["explosive"]),
        turnovers=len(groups["turnovers"]),
        by_down=[
            DownSummary(
                down=down,
                plays=int(row["plays"]),
                success_rate=_rate(int(row["success"]), int(row["graded"])),
                yards_per_play=_per(row["yards"], int(row["plays"])),
                rush_share=_rate(int(row["rush"]), int(row["plays"])),
                clip_ids=row["clip_ids"],
            )
            for down, row in sorted(downs.items())
        ],
        groups=[
            PlayGroup(key=key, label=labels[key], plays=len(ids), clip_ids=ids)
            for key, ids in groups.items()
            if ids
        ],
        play_log=log,
    )


def _play_clips(clips: list[Any]) -> dict[int, tuple[dict[str, Any], uuid.UUID]]:
    """One ``(cfbd, clip_id)`` per linked play, preferring the sideline angle."""
    by_play: dict[int, tuple[dict[str, Any], uuid.UUID, bool]] = {}
    for clip in sorted(clips, key=lambda c: c.start_time):
        label_data = clip.label_data or {}
        cfbd = label_data.get("cfbd")
        if clip.play_number is None or not isinstance(cfbd, dict):
            continue
        sideline = label_data.get("view") == "sideline"
        current = by_play.get(clip.play_number)
        if current is None or (sideline and not current[2]):
            by_play[clip.play_number] = (cfbd, clip.id, sideline)
    return {number: (cfbd, clip_id) for number, (cfbd, clip_id, _) in by_play.items()}


@router.get("/{video_id}/game-summary", response_model=GameSummary)
async def game_summary(
    video_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    _user: Annotated[User, Depends(require_any_staff)],
) -> GameSummary:
    """Toledo offense and defense for one linked game film."""
    video = await db.get(Video, video_id)
    if video is None:
        raise HTTPException(404, "Film not found")
    return await build_game_summary(db, video)


async def build_game_summary(db: AsyncSession, video: Video) -> GameSummary:
    """The game breakdown for ``video`` (also used by the team dashboard)."""
    video_id = video.id
    clips = list(
        (await db.execute(select(Clip).where(Clip.video_id == video_id, active_clip()))).scalars()
    )
    film_plays = len({c.play_number for c in clips if c.play_number is not None})
    linked = _play_clips(clips)
    ordered = [
        ({**linked[number][0], "_play_number": number}, linked[number][1])
        for number in sorted(linked)
    ]
    offense = [(p, c) for p, c in ordered if p.get("offense") == TOLEDO]
    defense = [(p, c) for p, c in ordered if p.get("offense") and p.get("offense") != TOLEDO]
    opponent = next(
        (
            p.get("defense") if p.get("offense") == TOLEDO else p.get("offense")
            for p, _ in ordered
            if p.get("offense")
        ),
        video.opponent_team,
    )
    return GameSummary(
        video_id=video_id,
        opponent=opponent,
        film_plays=film_plays,
        linked_plays=len(linked),
        offense=summarize_side(offense) if offense else None,
        defense=summarize_side(defense) if defense else None,
    )
