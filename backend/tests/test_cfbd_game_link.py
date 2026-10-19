"""Game film <-> CFBD play-by-play linking (Needleman-Wunsch alignment)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest_asyncio
from app.cfbd.game_link import (
    FilmPlay,
    OfficialPlay,
    align,
    ball_on,
    find_game,
    link_video,
    match_score,
)
from app.cfbd.models import CFBDGame, CFBDPlay
from app.config import get_settings
from app.database import Base
from app.models import Clip, SessionKind, SideOfBall, User, UserRole, Video
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


def _film(n: int, q: int | None, down: int | None, dist: int | None, spot: int | None) -> FilmPlay:
    return FilmPlay(n, q, down, dist, spot)


def _official(i: int, q: int, down: int, dist: int, yl: int, kind: str = "Rush") -> OfficialPlay:
    return OfficialPlay(i, q, down, dist, yl, kind)


def test_ball_on_folds_the_field() -> None:
    assert ball_on(75) == 25 and ball_on(30) == 30 and ball_on(50) == 50 and ball_on(None) is None


def test_exact_situation_scores_high_and_wrong_quarter_low() -> None:
    official = _official(0, 1, 2, 5, 70)
    assert match_score(_film(1, 1, 2, 5, 30), official) >= 6
    assert match_score(_film(1, 2, 2, 5, 30), official) < 3


def test_alignment_skips_replays_and_unfilmed_plays() -> None:
    # Real 2026 Toledo @ Michigan State opening (CFBD yard lines from the home goal).
    official = [
        _official(0, 1, 1, 10, 35, "Kickoff"),
        _official(1, 1, 1, 10, 75),
        _official(2, 1, 2, 5, 70, "Pass Incompletion"),
        _official(3, 1, 3, 5, 70, "Pass Incompletion"),
        _official(4, 1, 4, 5, 70, "Punt Return"),
    ]
    film = [
        _film(1, 1, None, None, None),  # kickoff card is blank
        _film(2, 1, 1, 10, 25),
        _film(3, 1, 2, 5, 30),
        _film(4, 1, 2, 5, 30),  # replay of the same snap
        # 3rd & 5 was not filmed
        _film(5, 1, 4, 5, 30),
    ]
    pairs = {film[i].play_number: official[j].index for i, j, _ in align(film, official)}
    assert pairs[1] == 0 and pairs[2] == 1 and pairs[5] == 4
    assert sorted(v for k, v in pairs.items() if k in (3, 4)) == [2]  # one of them only


def test_unrelated_sequences_do_not_force_matches() -> None:
    film = [_film(1, 3, 3, 8, 12)]
    official = [_official(0, 1, 1, 10, 75)]
    assert align(film, official) == []


def test_alignment_skips_below_threshold_pairs_before_matching() -> None:
    film = [_film(1, 2, 1, 10, 25), _film(2, 2, 1, 10, 30)]
    official = [_official(0, 1, 1, 10, 75), _official(1, 2, 1, 10, 75)]

    assert align(film, official) == [(0, 1, 6.5)]


@pytest_asyncio.fixture
async def db() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    schema = f"gamelink_{uuid.uuid4().hex}"
    engine = create_async_engine(
        get_settings().database_url,
        connect_args={"server_settings": {"search_path": f"{schema},public"}},
        execution_options={"schema_translate_map": {None: schema}},
    )
    async with engine.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await engine.dispose()


async def test_find_game_rejects_wrong_opponent_for_single_candidate(
    db: async_sessionmaker[AsyncSession],
) -> None:
    kickoff = datetime(2026, 9, 5, 23, 0, tzinfo=UTC)
    async with db() as s:
        s.add(
            CFBDGame(
                cfbd_game_id=401858429,
                season=2026,
                week=1,
                start_date=kickoff,
                home_team="Michigan State",
                away_team="Toledo",
            )
        )
        await s.commit()

        game = await find_game(
            s,
            SimpleNamespace(recorded_at=kickoff, opponent_team="Ohio"),
        )

    assert game is None


async def test_link_video_writes_official_results(db: async_sessionmaker[AsyncSession]) -> None:
    kickoff = datetime(2026, 9, 5, 23, 0, tzinfo=UTC)
    async with db() as s:
        user = User(
            id=uuid.uuid4(),
            email=f"g{uuid.uuid4().hex[:6]}@example.com",
            hashed_password="x",
            full_name="Coach",
            role=UserRole.coach,
        )
        video = Video(
            id=uuid.uuid4(),
            filename="26 01 OHTO FULL GAME @ MIST.mp4",
            storage_uri="s3://raw-video/raw/game.mp4",
            uploaded_by=user.id,
            session_kind=SessionKind.game,
            opponent_team="Michigan State",
            recorded_at=kickoff,
        )
        s.add_all([user, video])
        s.add(
            CFBDGame(
                cfbd_game_id=401858429,
                season=2026,
                week=1,
                start_date=kickoff,
                home_team="Michigan State",
                away_team="Toledo",
            )
        )
        rows = [
            (1, "Michigan State", 1, 10, 35, 0, "Kickoff"),
            (2, "Toledo", 1, 10, 75, 5, "Rush"),
            (3, "Toledo", 2, 5, 70, 0, "Pass Incompletion"),
            (4, "Toledo", 2, 5, 70, 0, "Timeout"),
        ]
        for pid, offense, down, dist, yl, gained, kind in rows:
            s.add(
                CFBDPlay(
                    cfbd_play_id=401858429000 + pid,
                    cfbd_game_id=401858429,
                    offense=offense,
                    defense="Toledo" if offense != "Toledo" else "Michigan State",
                    period=1,
                    down=down,
                    distance=dist,
                    yard_line=yl,
                    yards_gained=gained,
                    play_type=kind,
                    ppa=0.1 * pid,
                )
            )
        boards = {
            1: {"quarter": 1},
            2: {"quarter": 1, "down": 1, "distance": None, "ball_on": 25},
            3: {"quarter": 1, "down": 2, "distance": 5, "ball_on": 30},
        }
        for number, board in boards.items():
            for view_index, view in enumerate(("sideline", "endzone")):
                attributes = {}
                if number == 2 and view == "sideline":
                    attributes = {"down": 1, "our_possession": SideOfBall.defense}
                elif number == 2:
                    attributes = {"distance": 10}
                s.add(
                    Clip(
                        id=uuid.uuid4(),
                        video_id=video.id,
                        start_time=number * 30 + view_index * 12,
                        end_time=number * 30 + view_index * 12 + 10,
                        play_number=number,
                        label_data={"view": view, "scoreboard": board},
                        **attributes,
                    )
                )
        stale_clip = Clip(
            id=uuid.uuid4(),
            video_id=video.id,
            start_time=150,
            end_time=160,
            play_number=99,
            label_data={"view": "sideline", "custom": "preserve", "cfbd": {"play_id": -1}},
        )
        s.add(stale_clip)
        await s.commit()
        video_id = video.id
        stale_clip_id = stale_clip.id

    async with db() as s:
        result = await link_video(s, video_id)
        await s.commit()
    assert result["status"] == "linked"
    assert result["matched_plays"] == 3 and result["clips_updated"] == 7

    async with db() as s:
        clips = list((await s.execute(select(Clip).where(Clip.play_number == 2))).scalars())
        kickoff_clips = list((await s.execute(select(Clip).where(Clip.play_number == 1))).scalars())
        stale_clip = await s.get(Clip, stale_clip_id)
    assert len(clips) == 2
    for clip in clips:
        assert clip.label_data["cfbd"]["play_type"] == "Rush"
        assert clip.label_data["cfbd"]["yards_gained"] == 5
        assert clip.down == 1 and clip.distance == 10
    sideline = next(clip for clip in clips if clip.our_possession == SideOfBall.defense)
    endzone = next(clip for clip in clips if clip.our_possession == SideOfBall.offense)
    assert sideline.our_possession == SideOfBall.defense
    assert endzone.our_possession == SideOfBall.offense
    assert all(clip.our_possession == SideOfBall.special_teams for clip in kickoff_clips)
    assert stale_clip is not None
    assert stale_clip.label_data == {"view": "sideline", "custom": "preserve"}


async def test_ensure_game_fetches_the_schedule_when_the_game_is_not_cached(monkeypatch):
    from datetime import UTC, datetime
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from app.cfbd import client as client_module
    from app.cfbd import game_link
    from app.cfbd import sync as sync_module

    game = SimpleNamespace(cfbd_game_id=1)
    find = AsyncMock(side_effect=[None, game])
    synced = AsyncMock()
    monkeypatch.setattr(game_link, "find_game", find)
    monkeypatch.setattr(sync_module, "sync_games", synced)
    fake_client = MagicMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(
        client_module.CFBDClient, "from_settings", classmethod(lambda cls, **_: fake_client)
    )
    monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(cfbd_api_key="test-key"))

    video = SimpleNamespace(recorded_at=datetime(2026, 9, 5, tzinfo=UTC))
    assert await game_link.ensure_game(MagicMock(), video) is game
    assert synced.await_args.kwargs == {"season": 2026, "season_type": "regular"}

    # A January bowl belongs to the previous season.
    find.side_effect = [None, None]
    synced.reset_mock()
    bowl = SimpleNamespace(recorded_at=datetime(2027, 1, 2, tzinfo=UTC))
    assert await game_link.ensure_game(MagicMock(), bowl) is None
    assert [c.kwargs for c in synced.await_args_list] == [
        {"season": 2026, "season_type": "regular"},
        {"season": 2026, "season_type": "postseason"},
    ]
