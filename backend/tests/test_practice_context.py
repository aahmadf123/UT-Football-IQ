"""Real HTTP/DB context updates keep our practice units out of opponent film."""

import uuid

import httpx
import pytest
from app.database import get_db
from app.deps import get_current_user
from app.main import app
from app.models import Clip, SessionKind, Video
from sqlalchemy import select
from test_local_workspace import workspace_db as workspace_db


@pytest.mark.asyncio
async def test_recording_inherits_practice_and_moves_between_practice_and_game(
    workspace_db, monkeypatch
):
    maker, user, _ = workspace_db
    monkeypatch.setattr("app.storage.recording_fingerprint", lambda *_: ("c" * 64, 100))

    async def db_override():
        async with maker() as db:
            yield db
            await db.commit()

    app.dependency_overrides[get_db] = db_override
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            practice = (
                await client.post(
                    "/api/v1/sessions",
                    json={
                        "title": "Our Tuesday practice",
                        "session_kind": "practice",
                        "opponent_team": "Wrong",
                    },
                )
            ).json()
            assert practice["opponent_team"] is None
            created = await client.post(
                "/api/v1/videos",
                json={
                    "filename": "workspace-practice.mp4",
                    "storage_uri": "s3://raw-video/practice.mp4",
                    "practice_session_id": practice["id"],
                    "our_possession": "defense",
                },
            )
            assert created.status_code == 201, created.text
            video = created.json()
            assert (video["session_kind"], video["opponent_team"], video["our_possession"]) == (
                "practice",
                None,
                "defense",
            )
            async with maker() as db:
                clip = Clip(
                    id=uuid.uuid4(),
                    video_id=uuid.UUID(video["id"]),
                    start_time=0,
                    end_time=1,
                    session_kind=SessionKind.practice,
                )
                db.add(clip)
                await db.commit()
                clip_id = clip.id
            game = (
                await client.post(
                    "/api/v1/sessions",
                    json={
                        "title": "Game",
                        "session_kind": "game",
                        "opponent_team": "Real opponent",
                    },
                )
            ).json()
            for target, kind, opponent in [
                (game, "game", "Real opponent"),
                (practice, "practice", None),
            ]:
                moved = await client.post(
                    f"/api/v1/sessions/{target['id']}/recordings", json={"video_id": video["id"]}
                )
                assert moved.status_code == 200, moved.text
                saved = (await client.get(f"/api/v1/videos/{video['id']}")).json()
                assert (saved["session_kind"], saved["opponent_team"], saved["our_possession"]) == (
                    kind,
                    opponent,
                    "defense",
                )
                async with maker() as db:
                    clip = await db.get(Clip, clip_id)
                    assert clip.session_kind.value == kind
            changed = await client.patch(
                f"/api/v1/sessions/{practice['id']}",
                json={"session_kind": "game", "opponent_team": "Next opponent"},
            )
            assert changed.status_code == 200, changed.text
            async with maker() as db:
                saved = (
                    await db.execute(select(Video).where(Video.id == uuid.UUID(video["id"])))
                ).scalar_one()
                assert (
                    saved.session_kind == SessionKind.game
                    and saved.opponent_team == "Next opponent"
                )
    finally:
        app.dependency_overrides.clear()
