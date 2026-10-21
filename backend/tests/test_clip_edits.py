"""Real PostgreSQL split/merge/undo and preserved canonical lineage contracts."""

import uuid
from collections.abc import AsyncGenerator

import pytest
from app.config import get_settings
from app.database import get_db
from app.deps import get_current_user
from app.main import app
from app.models import Clip, FootballSession, Metric, User, UserRole, Video, VideoStatus
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine


@pytest.fixture
async def workspace() -> AsyncGenerator[tuple[AsyncClient, AsyncSession, FootballSession], None]:
    engine = create_async_engine(get_settings().database_url)
    async with engine.connect() as connection:
        transaction = await connection.begin()
        async with AsyncSession(bind=connection, expire_on_commit=False) as db:
            user = User(
                id=uuid.uuid4(),
                email=f"edit-{uuid.uuid4()}@example.invalid",
                full_name="Clip editor",
                hashed_password="unused-test-hash",
                role=UserRole.analyst,
            )
            session = FootballSession(id=uuid.uuid4(), title="Structural edit test")
            db.add_all([user, session])
            await db.flush()

            async def override_db() -> AsyncGenerator[AsyncSession, None]:
                yield db

            app.dependency_overrides[get_db] = override_db
            app.dependency_overrides[get_current_user] = lambda: user
            try:
                async with AsyncClient(
                    transport=ASGITransport(app=app), base_url="http://test"
                ) as client:
                    yield client, db, session
            finally:
                app.dependency_overrides.clear()
        await transaction.rollback()
    await engine.dispose()


async def film(
    db: AsyncSession, session: FootballSession, windows: list[tuple[float, float]]
) -> tuple[Video, list[Clip]]:
    video = Video(
        id=uuid.uuid4(),
        filename=f"edit-{uuid.uuid4()}.mp4",
        storage_uri=f"local://raw-video/{uuid.uuid4()}.mp4",
        status=VideoStatus.ready,
        practice_session_id=session.id,
        duration_seconds=120,
    )
    db.add(video)
    await db.flush()
    clips = [
        Clip(
            id=uuid.uuid4(),
            video_id=video.id,
            start_time=start,
            end_time=end,
            label_data={"_annotation_revision": 0, "canonical_count_eligible": True},
            provenance={"source": "real"},
        )
        for start, end in windows
    ]
    db.add_all(clips)
    await db.flush()
    return video, clips


async def test_split_and_undo_preserve_originals_metrics_and_deep_links(workspace) -> None:
    client, db, session = workspace
    video, clips = await film(db, session, [(0, 5)])
    original = clips[0]
    metric = Metric(
        id=uuid.uuid4(),
        clip_id=original.id,
        metric_name="distance_traveled",
        metric_value={"yards": 4},
        analytics_safe=True,
        provenance={"source": "human"},
    )
    db.add(metric)
    await db.flush()
    response = await client.post(
        f"/api/v1/clips/{original.id}/split", json={"at_seconds": 2, "expected_revision": 0}
    )
    assert response.status_code == 200, response.text
    edit = response.json()
    children = edit["clips"]
    assert [(clip["start_time"], clip["end_time"]) for clip in children] == [(0, 2), (2, 5)]
    assert all(clip["storage_uri"] is None for clip in children)
    assert len({clip["canonical_play_id"] for clip in children}) == 2
    listed = (await client.get(f"/api/v1/videos/{video.id}/clips")).json()
    assert {clip["id"] for clip in listed} == {clip["id"] for clip in children}
    old = (await client.get(f"/api/v1/clips/{original.id}")).json()
    assert old["provenance"]["state"] == "superseded"
    assert (await client.get("/api/v1/metrics", params={"clip_id": str(original.id)})).json() == []
    cohort = (
        await client.post("/api/v1/analytics/query", json={"session_ids": [str(session.id)]})
    ).json()
    assert cohort["cohort"]["plays"] == 2 and cohort["cohort"]["eligible_measurements"] == 0
    overrides = (await client.get(f"/api/v1/videos/{video.id}/boundary-overrides")).json()
    assert overrides["active"] and len(overrides["clips"]) == 2
    history = (await client.get(f"/api/v1/clips/{children[0]['id']}/edits")).json()
    assert history[0]["operation_id"] == edit["operation_id"] and history[0]["undo_available"]
    undone = await client.post(
        f"/api/v1/clip-edits/{edit['operation_id']}/undo",
        json={"expected_revisions": edit["revisions"]},
    )
    assert undone.status_code == 200, undone.text
    listed = (await client.get(f"/api/v1/videos/{video.id}/clips")).json()
    assert len(listed) == 1 and listed[0]["id"] == str(original.id)
    assert (await client.get(f"/api/v1/clips/{children[0]['id']}")).status_code == 200
    restored = (await client.get("/api/v1/metrics", params={"clip_id": str(original.id)})).json()
    assert len(restored) == 1 and restored[0]["id"] == str(metric.id)
    assert await db.get(Metric, metric.id) is not None


async def test_undo_conflicts_with_later_revision_and_preserves_correction(workspace) -> None:
    client, db, session = workspace
    _video, clips = await film(db, session, [(0, 5)])
    edit = (
        await client.post(
            f"/api/v1/clips/{clips[0].id}/split", json={"at_seconds": 2, "expected_revision": 0}
        )
    ).json()
    child = edit["clips"][0]
    changed = await client.patch(
        f"/api/v1/clips/{child['id']}", json={"expected_revision": 1, "personnel_grouping": "11"}
    )
    assert changed.status_code == 200, changed.text
    revisions = {**edit["revisions"], child["id"]: changed.json()["annotation_revision"]}
    undo = await client.post(
        f"/api/v1/clip-edits/{edit['operation_id']}/undo", json={"expected_revisions": revisions}
    )
    assert undo.status_code == 409
    current = (await client.get(f"/api/v1/clips/{child['id']}")).json()
    assert current["personnel_grouping"] == "11"


@pytest.mark.parametrize("windows", [[(0, 5), (7, 12)], [(0, 5), (4, 9)]])
async def test_merge_handles_adjacent_gap_or_overlap(workspace, windows) -> None:
    client, db, session = workspace
    video, clips = await film(db, session, windows)
    result = await client.post(
        "/api/v1/clips/merge",
        json={
            "clip_ids": [str(clip.id) for clip in clips],
            "expected_revisions": {str(clip.id): 0 for clip in clips},
        },
    )
    assert result.status_code == 200, result.text
    merged = result.json()["clips"][0]
    assert merged["start_time"] == 0 and merged["end_time"] == max(end for _start, end in windows)
    assert len((await client.get(f"/api/v1/videos/{video.id}/clips")).json()) == 1


async def test_merge_rejects_skipped_play_or_different_recordings(workspace) -> None:
    client, db, session = workspace
    _video, clips = await film(db, session, [(0, 5), (7, 12), (14, 19)])
    _other, other_clips = await film(db, session, [(0, 5)])
    for ids, status in (([clips[0].id, clips[2].id], 409), ([clips[0].id, other_clips[0].id], 422)):
        response = await client.post(
            "/api/v1/clips/merge",
            json={
                "clip_ids": [str(value) for value in ids],
                "expected_revisions": {str(value): 0 for value in ids},
            },
        )
        assert response.status_code == status, response.text


@pytest.mark.parametrize("target_existing", [False, True])
async def test_manual_camera_match_blocks_undo_and_inactive_matching(
    workspace, target_existing
) -> None:
    client, db, session = workspace
    _video, clips = await film(db, session, [(0, 5)])
    _other, other_clips = await film(db, session, [(0, 5)])
    edit = (
        await client.post(
            f"/api/v1/clips/{clips[0].id}/split", json={"at_seconds": 2, "expected_revision": 0}
        )
    ).json()
    original_match = await client.post(
        f"/api/v1/sessions/{session.id}/plays/match",
        json={"clip_ids": [str(clips[0].id), str(other_clips[0].id)]},
    )
    assert original_match.status_code == 409
    child_id = edit["clips"][0]["id"]
    matched = await client.post(
        f"/api/v1/sessions/{session.id}/plays/match",
        json={
            "clip_ids": [str(other_clips[0].id)],
            "play_id": edit["clips"][0]["canonical_play_id"],
        }
        if target_existing
        else {"clip_ids": [child_id, str(other_clips[0].id)]},
    )
    assert matched.status_code == 200, matched.text
    undo = await client.post(
        f"/api/v1/clip-edits/{edit['operation_id']}/undo",
        json={"expected_revisions": edit["revisions"]},
    )
    assert undo.status_code == 409


@pytest.mark.parametrize("same_view", [True, False])
async def test_boundary_edits_preserve_only_unanimous_view_metadata(workspace, same_view) -> None:
    client, db, session = workspace
    _video, clips = await film(db, session, [(0, 5), (7, 12)])
    from app.models import CaptureRegime

    clips[0].capture_regime = CaptureRegime.fixed_sideline
    clips[0].label_data = {**clips[0].label_data, "view": "sideline"}
    clips[1].capture_regime = (
        CaptureRegime.fixed_sideline if same_view else CaptureRegime.fixed_endzone
    )
    clips[1].label_data = {**clips[1].label_data, "view": "sideline" if same_view else "endzone"}
    await db.flush()
    response = await client.post(
        "/api/v1/clips/merge",
        json={
            "clip_ids": [str(c.id) for c in clips],
            "expected_revisions": {str(c.id): 0 for c in clips},
        },
    )
    assert response.status_code == 200, response.text
    child = response.json()["clips"][0]
    assert child["label_data"].get("view") == ("sideline" if same_view else None)
    assert child["capture_regime"] == ("fixed_sideline" if same_view else None)


async def test_stale_segmenter_cannot_add_windows_after_staff_edit(workspace) -> None:
    client, db, session = workspace
    video, clips = await film(db, session, [(0, 5)])
    edit = (
        await client.post(
            f"/api/v1/clips/{clips[0].id}/split", json={"at_seconds": 2, "expected_revision": 0}
        )
    ).json()
    stale = await client.post(
        f"/api/v1/videos/{video.id}/clips",
        json={
            "id": str(uuid.uuid4()),
            "start_time": 0,
            "end_time": 5.1,
            "boundary_source": "model",
            "provenance": {"source": "real"},
        },
    )
    assert stale.status_code == 409, stale.text
    listed = (await client.get(f"/api/v1/videos/{video.id}/clips")).json()
    assert {row["id"] for row in listed} == {row["id"] for row in edit["clips"]}
    replay = await client.post(
        f"/api/v1/videos/{video.id}/clips",
        json={
            "id": edit["clips"][0]["id"],
            "start_time": 0,
            "end_time": 5.1,
            "boundary_source": "model",
        },
    )
    assert replay.status_code == 201 and replay.json()["end_time"] == 2


async def test_nested_structural_undo_restores_parent_undo(workspace) -> None:
    client, db, session = workspace
    video, clips = await film(db, session, [(0, 5)])
    split = (
        await client.post(
            f"/api/v1/clips/{clips[0].id}/split", json={"at_seconds": 2, "expected_revision": 0}
        )
    ).json()
    merge = await client.post(
        "/api/v1/clips/merge",
        json={
            "clip_ids": [c["id"] for c in split["clips"]],
            "expected_revisions": split["revisions"],
        },
    )
    assert merge.status_code == 200, merge.text
    merged = merge.json()
    undone_merge = await client.post(
        f"/api/v1/clip-edits/{merged['operation_id']}/undo",
        json={"expected_revisions": merged["revisions"]},
    )
    assert undone_merge.status_code == 200, undone_merge.text
    history = (await client.get(f"/api/v1/clips/{clips[0].id}/edits")).json()
    parent = next(row for row in history if row["operation_id"] == split["operation_id"])
    assert parent["undo_available"]
    undone_split = await client.post(
        f"/api/v1/clip-edits/{split['operation_id']}/undo",
        json={"expected_revisions": parent["revisions"]},
    )
    assert undone_split.status_code == 200, undone_split.text
    listed = (await client.get(f"/api/v1/videos/{video.id}/clips")).json()
    assert [row["id"] for row in listed] == [str(clips[0].id)]
    assert (await client.get(f"/api/v1/videos/{video.id}/boundary-overrides")).json()["active"]
