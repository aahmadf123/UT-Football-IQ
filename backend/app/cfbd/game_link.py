"""Link edited game-film plays to CFBD play-by-play.

Game film arrives as one clip per camera angle per play, and the worker reads
each play's stadium scoreboard card (quarter, clock, down & distance, "ball
on"). CFBD has the official play-by-play for the same game: play type, yards
gained, result text and PPA (EPA). Matching them gives every clip its
official result.

The two sequences are in the same order but don't correspond one-to-one: the
film skips timeouts and period ends CFBD lists, shows some plays twice
(replays), and sometimes misses one. So they are aligned like DNA sequences
(Needleman-Wunsch): matching a film play to a CFBD play scores agreement on
quarter, down, distance and field position; skipping either side costs a
little. CFBD's clock is not used -- it repeats the drive's start time on
every play.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import structlog
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cfbd.models import CFBDGame, CFBDPlay

log = structlog.get_logger("app.cfbd.game_link")

TOLEDO = "Toledo"
#: CFBD rows that are not snaps the film would show.
NON_PLAYS = {"Timeout", "End Period", "End of Half", "End of Game", "End of Regulation"}
#: Plays whose scoreboard card usually shows no down (kickoffs, tries).
NO_DOWN_PLAYS = {
    "Kickoff",
    "Kickoff Return (Offense)",
    "Extra Point Good",
    "Extra Point Missed",
    "Two Point Pass",
    "Two Point Rush",
    "Defensive 2pt Conversion",
}
MIN_MATCH_SCORE = 2.0
SKIP_FILM = -1.0
SKIP_CFBD = -0.5
SPECIAL_TEAMS_PLAY_TYPE_MARKERS = (
    "kickoff",
    "punt",
    "field goal",
    "extra point",
    "two point",
    "2pt",
    "2-point",
    "try",
)


@dataclass(frozen=True)
class FilmPlay:
    play_number: int
    quarter: int | None
    down: int | None
    distance: int | None
    ball_on: int | None


@dataclass(frozen=True)
class OfficialPlay:
    index: int
    period: int | None
    down: int | None
    distance: int | None
    yard_line: int | None
    play_type: str | None


def ball_on(yard_line: int | None) -> int | None:
    """The scoreboard's "ball on" number for a CFBD yard line (0-100)."""
    if yard_line is None:
        return None
    return min(yard_line, 100 - yard_line)


def match_score(film: FilmPlay, official: OfficialPlay) -> float:
    """How well one film play agrees with one CFBD play (higher is better)."""
    score = 0.0
    if film.quarter is not None and official.period is not None:
        score += 1.0 if film.quarter == official.period else -4.0
    no_down = official.play_type in NO_DOWN_PLAYS
    if film.down is None:
        score += 1.0 if no_down else -0.5
    elif official.down is not None and not no_down:
        # A different down is a different snap, however close the rest is.
        score += 2.0 if film.down == official.down else -4.0
        if film.distance is not None and official.distance is not None:
            score += 1.5 if film.distance == official.distance else -1.0
    spot = ball_on(official.yard_line)
    if film.ball_on is not None and spot is not None:
        gap = abs(film.ball_on - spot)
        score += 2.0 if gap == 0 else 1.0 if gap <= 1 else -1.0
    return score


def align(film: list[FilmPlay], official: list[OfficialPlay]) -> list[tuple[int, int, float]]:
    """Order-preserving best alignment: ``(film_index, official_index, score)``.

    Only pairs scoring at least ``MIN_MATCH_SCORE`` are returned; everything
    else is treated as unmatched rather than guessed.
    """
    n, m = len(film), len(official)
    best = [[0.0] * (m + 1) for _ in range(n + 1)]
    move = [[0] * (m + 1) for _ in range(n + 1)]  # 0 match, 1 skip film, 2 skip cfbd
    for i in range(1, n + 1):
        best[i][0] = best[i - 1][0] + SKIP_FILM
        move[i][0] = 1
    for j in range(1, m + 1):
        best[0][j] = best[0][j - 1] + SKIP_CFBD
        move[0][j] = 2
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            score = match_score(film[i - 1], official[j - 1])
            options = (
                best[i - 1][j - 1] + score if score >= MIN_MATCH_SCORE else float("-inf"),
                best[i - 1][j] + SKIP_FILM,
                best[i][j - 1] + SKIP_CFBD,
            )
            k = max(range(3), key=lambda idx: options[idx])
            best[i][j], move[i][j] = options[k], k
    pairs: list[tuple[int, int, float]] = []
    i, j = n, m
    while i > 0 and j > 0:
        if move[i][j] == 0:
            score = match_score(film[i - 1], official[j - 1])
            if score >= MIN_MATCH_SCORE:
                pairs.append((i - 1, j - 1, score))
            i, j = i - 1, j - 1
        elif move[i][j] == 1:
            i -= 1
        else:
            j -= 1
    return list(reversed(pairs))


def film_plays_from_clips(clips: list[Any]) -> list[FilmPlay]:
    """One entry per play number, from the scoreboard reading the worker
    stored on that play's clips (``label_data.scoreboard``)."""
    by_play: dict[int, FilmPlay] = {}
    for clip in sorted(clips, key=lambda c: (c.play_number or 0, c.start_time)):
        if clip.play_number is None or clip.play_number in by_play:
            continue
        board = (clip.label_data or {}).get("scoreboard") or {}
        by_play[clip.play_number] = FilmPlay(
            play_number=clip.play_number,
            quarter=board.get("quarter"),
            down=board.get("down"),
            distance=board.get("distance"),
            ball_on=board.get("ball_on"),
        )
    return [by_play[k] for k in sorted(by_play)]


async def find_game(db: AsyncSession, video: Any) -> CFBDGame | None:
    """Toledo's CFBD game on the film's date (+/- 2 days)."""
    if video.recorded_at is None:
        return None
    window = (video.recorded_at - timedelta(days=2), video.recorded_at + timedelta(days=2))
    games = list(
        (
            await db.execute(
                select(CFBDGame).where(
                    or_(CFBDGame.home_team == TOLEDO, CFBDGame.away_team == TOLEDO),
                    CFBDGame.start_date.between(*window),
                )
            )
        ).scalars()
    )
    if video.opponent_team:
        opponent = video.opponent_team.lower()
        games = [
            g
            for g in games
            if opponent in (g.home_team or "").lower() or opponent in (g.away_team or "").lower()
        ]
    return games[0] if games else None


async def link_video(db: AsyncSession, video_id: uuid.UUID) -> dict[str, Any]:
    """Match a game video's plays to CFBD and record the official result on
    each clip (``label_data.cfbd``, plus down/distance/possession when the
    scoreboard didn't provide them). Idempotent; the caller commits."""
    from app.models import Clip, SideOfBall, Video

    video = await db.get(Video, video_id)
    if video is None:
        return {"status": "not_found"}
    game = await find_game(db, video)
    if game is None:
        return {"status": "no_game", "reason": "No CFBD game for Toledo on this film's date."}
    rows = list(
        (
            await db.execute(
                select(CFBDPlay)
                .where(CFBDPlay.cfbd_game_id == game.cfbd_game_id)
                .order_by(CFBDPlay.cfbd_play_id)
            )
        ).scalars()
    )
    plays = [p for p in rows if p.play_type not in NON_PLAYS]
    if not plays:
        return {
            "status": "no_plays",
            "game_id": game.cfbd_game_id,
            "reason": "CFBD play-by-play for this game isn't cached yet.",
        }
    clips = list((await db.execute(select(Clip).where(Clip.video_id == video_id))).scalars())
    film = film_plays_from_clips(clips)
    official = [
        OfficialPlay(i, p.period, p.down, p.distance, p.yard_line, p.play_type)
        for i, p in enumerate(plays)
    ]
    pairs = align(film, official)
    by_number = {film[i].play_number: (plays[j], score) for i, j, score in pairs}
    updated = 0
    for clip in clips:
        match = by_number.get(clip.play_number) if clip.play_number is not None else None
        if match is None:
            label_data = dict(clip.label_data or {})
            if "cfbd" in label_data:
                label_data.pop("cfbd")
                clip.label_data = label_data
                updated += 1
            continue
        play, score = match
        label_data = dict(clip.label_data or {})
        label_data["cfbd"] = {
            "play_id": play.cfbd_play_id,
            "game_id": play.cfbd_game_id,
            "offense": play.offense,
            "defense": play.defense,
            # Home teams wear dark jerseys by default; the worker uses this
            # to tell which colour is Toledo when it labels teams.
            "home_team": game.home_team,
            "period": play.period,
            "down": play.down,
            "distance": play.distance,
            "yard_line": play.yard_line,
            "yards_gained": play.yards_gained,
            "play_type": play.play_type,
            "play_text": play.play_text,
            "ppa": play.ppa,
            "scoring": play.scoring,
            "match_score": round(score, 2),
        }
        if play.offense:
            # Possession is now known (see clips.create: pending until linked).
            label_data.pop("possession_pending", None)
        clip.label_data = label_data
        if clip.down is None and play.down is not None and play.play_type not in NO_DOWN_PLAYS:
            clip.down = play.down
        if (
            clip.distance is None
            and play.distance is not None
            and play.play_type not in NO_DOWN_PLAYS
        ):
            clip.distance = play.distance
        if (
            clip.our_possession is None
            and play.play_type
            and any(
                marker in play.play_type.casefold() for marker in SPECIAL_TEAMS_PLAY_TYPE_MARKERS
            )
        ):
            clip.our_possession = SideOfBall.special_teams
        elif clip.our_possession is None and play.offense:
            clip.our_possession = (
                SideOfBall.offense if play.offense == TOLEDO else SideOfBall.defense
            )
        updated += 1
    log.info(
        "cfbd_game_linked",
        video_id=str(video_id),
        game_id=game.cfbd_game_id,
        film_plays=len(film),
        cfbd_plays=len(plays),
        matched=len(pairs),
        clips=updated,
    )
    return {
        "status": "linked",
        "game_id": game.cfbd_game_id,
        "opponent": game.away_team if game.home_team == TOLEDO else game.home_team,
        "film_plays": len(film),
        "cfbd_plays": len(plays),
        "matched_plays": len(pairs),
        "clips_updated": updated,
    }


async def ensure_game(db: AsyncSession, video: Any) -> CFBDGame | None:
    """Like :func:`find_game`, but fetches the season schedule from CFBD
    first when the game isn't cached (production may never have synced it).
    Needs a CFBD key; network errors leave whatever is cached.
    """
    from app.cfbd.client import CFBDClient
    from app.cfbd.sync import sync_games
    from app.config import get_settings

    game = await find_game(db, video)
    if game is not None or video.recorded_at is None:
        return game
    if not get_settings().cfbd_api_key.strip():
        return None
    played = video.recorded_at
    # January bowl games belong to the previous season.
    season = played.year if played.month >= 3 else played.year - 1
    season_types = ["regular", "postseason"] if played.month in (12, 1) else ["regular"]
    async with CFBDClient.from_settings(timeout=20.0, max_retries=2) as client:
        for season_type in season_types:
            await sync_games(db, client, season=season, season_type=season_type)
    return await find_game(db, video)


async def ensure_plays(db: AsyncSession, game: CFBDGame) -> bool:
    """Cache CFBD play-by-play for ``game``'s week if it isn't yet.

    Returns whether plays are available. Needs a CFBD key; network errors
    leave whatever is cached.
    """
    from sqlalchemy import func

    from app.cfbd.client import CFBDClient
    from app.cfbd.sync import sync_plays
    from app.config import get_settings

    cached = (
        await db.execute(
            select(func.count())
            .select_from(CFBDPlay)
            .where(CFBDPlay.cfbd_game_id == game.cfbd_game_id)
        )
    ).scalar_one()
    if cached:
        return True
    if not get_settings().cfbd_api_key.strip() or game.season is None or game.week is None:
        return False
    async with CFBDClient.from_settings(timeout=20.0, max_retries=2) as client:
        await sync_plays(
            db,
            client,
            season=game.season,
            season_type=game.season_type or "regular",
            weeks=[game.week],
        )
    cached = (
        await db.execute(
            select(func.count())
            .select_from(CFBDPlay)
            .where(CFBDPlay.cfbd_game_id == game.cfbd_game_id)
        )
    ).scalar_one()
    return bool(cached)
