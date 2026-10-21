"""Real PostgreSQL split/merge/undo and preserved canonical lineage contracts."""

import uuid
from collections.abc import AsyncGenerator

import pytest
from app.config import get_settings
from app.database import get_db
from app.deps import get_current_user
from app.main import app
from app.models import (
    Clip,
    FootballSession,
    Label,
    Metric,
    Tracklet,
    User,
    UserRole,
    Video,
    VideoStatus,
)
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
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


def generation(clips, windows, generation_id="unit-test-generation"):
    return {
        "generation_id": generation_id,
        "expected_active_clip_ids": [str(clip.id) for clip in clips],
        "clips": [
            {
                "start_time": start,
                "end_time": end,
                "boundary_source": "heuristic",
                "label_data": {"canonical_count_eligible": False},
            }
            for start, end in windows
        ],
    }


async def test_atomic_machine_layout_retires_old_windows_and_retries_without_duplicates(workspace):
    client, db, session = workspace
    video, clips = await film(db, session, [(0, 5), (5, 10)])
    video.duration_seconds = 10
    for clip in clips:
        clip.boundary_source = "heuristic"
    await db.flush()
    body = generation(clips, [(0, 6), (6, 10)])
    response = await client.post(f"/api/v1/videos/{video.id}/clips/batch", json=body)
    assert response.status_code == 200, response.text
    published = response.json()["clips"]
    assert len(published) == 2
    assert {c["id"] for c in published}.isdisjoint(str(c.id) for c in clips)
    active = (await client.get(f"/api/v1/videos/{video.id}/clips")).json()
    assert {c["id"] for c in active} == {c["id"] for c in published}
    original = (await client.get(f"/api/v1/clips/{clips[0].id}")).json()
    assert original["provenance"]["state"] == "superseded"
    retry = await client.post(f"/api/v1/videos/{video.id}/clips/batch", json=body)
    assert retry.status_code == 200
    assert [c["id"] for c in retry.json()["clips"]] == [c["id"] for c in published]
    assert len((await client.get(f"/api/v1/videos/{video.id}/clips")).json()) == 2


async def test_invalid_or_stale_whole_layout_preserves_every_old_clip(workspace):
    client, db, session = workspace
    video, clips = await film(db, session, [(0, 5), (5, 10)])
    video.duration_seconds = 10
    for clip in clips:
        clip.boundary_source = "heuristic"
    await db.flush()
    for body, code in [
        (generation(clips, [(0, 6), (6, 121)]), 422),
        (generation(clips, [(0, 6)]), 422),
        ({**generation(clips, [(0, 6), (6, 10)]), "expected_active_clip_ids": []}, 409),
    ]:
        response = await client.post(f"/api/v1/videos/{video.id}/clips/batch", json=body)
        assert response.status_code == code, response.text
        active = (await client.get(f"/api/v1/videos/{video.id}/clips")).json()
        assert {c["id"] for c in active} == {str(c.id) for c in clips}


async def test_human_identity_or_qualified_measurement_prevents_machine_recut(workspace):
    client, db, session = workspace
    video, clips = await film(db, session, [(0, 5)])
    clips[0].boundary_source = "heuristic"
    label = Label(
        id=uuid.uuid4(),
        clip_id=clips[0].id,
        label_type="player_identity",
        label_value={"player_id": "unit-fixture"},
        source="human",
    )
    db.add(label)
    await db.flush()
    response = await client.post(
        f"/api/v1/videos/{video.id}/clips/batch", json=generation(clips, [(0, 120)])
    )
    assert response.status_code == 409, response.text
    assert (await db.get(Label, label.id)) is label
    assert (await client.get(f"/api/v1/clips/{clips[0].id}")).json()["end_time"] == 5
    await db.delete(label)
    db.add(
        Metric(
            id=uuid.uuid4(),
            clip_id=clips[0].id,
            metric_name="distance_traveled",
            metric_value={"yards": 4},
            analytics_safe=True,
            experimental_flag=False,
            provenance={"source": "human"},
        )
    )
    await db.flush()
    response = await client.post(
        f"/api/v1/videos/{video.id}/clips/batch", json=generation(clips, [(0, 120)])
    )
    assert response.status_code == 409, response.text


async def test_confirmed_camera_views_share_canonical_play_after_atomic_batch(workspace):
    client, db, session = workspace
    video, clips = await film(db, session, [])
    video.duration_seconds = 10
    await db.flush()
    body = generation(clips, [(0, 5), (5, 10)])
    for index, proposal in enumerate(body["clips"]):
        proposal["play_call_id"] = "human-confirmed-play"
        proposal["label_data"] = {
            "view": ["sideline", "endzone"][index],
            "canonical_count_eligible": True,
            "contributes_snap_count": index == 0,
        }
    response = await client.post(f"/api/v1/videos/{video.id}/clips/batch", json=body)
    assert response.status_code == 200, response.text
    listed = (await client.get(f"/api/v1/videos/{video.id}/clips")).json()
    assert len({clip["canonical_play_id"] for clip in listed}) == 1
    assert all(len(clip["matched_views"]) == 2 for clip in listed)
    cohort = (
        await client.post("/api/v1/analytics/query", json={"session_ids": [str(session.id)]})
    ).json()
    assert cohort["cohort"]["plays"] == 1


async def test_unresolved_source_windows_remain_watchable_without_becoming_session_plays(workspace):
    client, db, session = workspace
    video, clips = await film(db, session, [])
    video.duration_seconds = 10
    await db.flush()
    assert (
        await client.post(
            f"/api/v1/videos/{video.id}/clips/batch", json=generation(clips, [(0, 5), (5, 10)])
        )
    ).status_code == 200
    response = (await client.get(f"/api/v1/sessions/{session.id}")).json()
    assert response["play_count"] == 0
    assert response["source_window_count"] == 2
    assert len(response["plays"]) == 2
    assert all(play["count_eligible"] is False for play in response["plays"])


async def test_mid_batch_failure_restores_original_layout_and_creates_no_partial_generation(
    workspace, monkeypatch
):
    from app.routers import clips as clip_routes

    client, db, session = workspace
    video, clips = await film(db, session, [(0, 5), (5, 10)])
    video.duration_seconds = 10
    for clip in clips:
        clip.boundary_source = "heuristic"
    await db.flush()
    video_id, old_ids = video.id, {str(clip.id) for clip in clips}
    build = clip_routes._build_clip
    built = 0

    def fail_second(*args):
        nonlocal built
        built += 1
        if built == 2:
            raise RuntimeError("Injected second-row publication failure")
        return build(*args)

    monkeypatch.setattr(clip_routes, "_build_clip", fail_second)
    with pytest.raises(RuntimeError, match="second-row"):
        await client.post(
            f"/api/v1/videos/{video_id}/clips/batch", json=generation(clips, [(0, 6), (6, 10)])
        )
    active = (await client.get(f"/api/v1/videos/{video_id}/clips")).json()
    assert {clip["id"] for clip in active} == old_ids
    assert (
        await db.execute(select(func.count()).select_from(Clip).where(Clip.video_id == video_id))
    ).scalar_one() == 2


async def test_published_fast_tracking_is_authoritative_even_without_qualified_metrics(workspace):
    client, db, session = workspace
    video, clips = await film(db, session, [(0, 5)])
    clips[0].boundary_source = "heuristic"
    db.add(
        Tracklet(
            id=uuid.uuid4(),
            clip_id=clips[0].id,
            start_frame=0,
            end_frame=30,
            provenance={"source": "real", "publication_state": "current"},
        )
    )
    await db.flush()
    override = (await client.get(f"/api/v1/videos/{video.id}/boundary-overrides")).json()
    assert override["active"] is True
    assert [clip["id"] for clip in override["clips"]] == [str(clips[0].id)]
    replacement = await client.post(
        f"/api/v1/videos/{video.id}/clips/batch", json=generation(clips, [(0, 120)])
    )
    assert replacement.status_code == 409
    assert (await client.get(f"/api/v1/clips/{clips[0].id}")).json()["end_time"] == 5


async def test_stale_preserved_ids_reject_new_staff_edits_events_labels_and_metrics(workspace):
    client, db, session = workspace
    video, clips = await film(db, session, [(0, 5), (5, 10)])
    video.duration_seconds = 10
    for clip in clips:
        clip.boundary_source = "heuristic"
    await db.flush()
    old_id = str(clips[0].id)
    assert (
        await client.post(
            f"/api/v1/videos/{video.id}/clips/batch", json=generation(clips, [(0, 6), (6, 10)])
        )
    ).status_code == 200
    attempts = [
        ("POST", "/api/v1/tracklets", {"clip_id": old_id, "start_frame": 0, "end_frame": 10}),
        ("PATCH", f"/api/v1/clips/{old_id}", {"down": 2}),
        (
            "POST",
            "/api/v1/events",
            {"clip_id": old_id, "event_type": "snap", "timestamp_seconds": 2},
        ),
        (
            "POST",
            "/api/v1/labels",
            {
                "clip_id": old_id,
                "label_type": "formation",
                "label_value": {"formation": "Trips"},
                "source": "human",
            },
        ),
        (
            "POST",
            "/api/v1/metrics",
            {
                "clip_id": old_id,
                "metric_name": "distance_traveled",
                "metric_value": {"yards": 4},
                "provenance": {"source": "human"},
            },
        ),
    ]
    for method, path, payload in attempts:
        response = await client.request(method, path, json=payload)
        assert response.status_code == 409, (path, response.text)
    assert (await client.get(f"/api/v1/clips/{old_id}")).status_code == 200


async def test_duplicate_output_ids_and_hidden_replacement_state_cannot_corrupt_active_layout(
    workspace,
):
    client, db, session = workspace
    video, clips = await film(db, session, [])
    video.duration_seconds = 10
    await db.flush()
    body = generation(clips, [(0, 5), (5, 10)])
    duplicated = str(uuid.uuid4())
    for item in body["clips"]:
        item["id"] = duplicated
    assert (
        await client.post(f"/api/v1/videos/{video.id}/clips/batch", json=body)
    ).status_code == 422
    assert (await client.get(f"/api/v1/videos/{video.id}/clips")).json() == []
    body = generation(clips, [(0, 5), (5, 10)])
    for item in body["clips"]:
        item["provenance"] = {"source": "heuristic", "state": "superseded"}
    assert (
        await client.post(f"/api/v1/videos/{video.id}/clips/batch", json=body)
    ).status_code == 200
    active = (await client.get(f"/api/v1/videos/{video.id}/clips")).json()
    assert len(active) == 2 and all(clip["provenance"]["state"] == "current" for clip in active)


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


async def test_supplied_id_colliding_with_another_generated_id_is_rejected_atomically(workspace):
    client, db, session = workspace
    video, clips = await film(db, session, [(0, 5), (5, 10)])
    video.duration_seconds = 10
    for clip in clips:
        clip.boundary_source = "heuristic"
    await db.flush()
    body = generation(clips, [(0, 6), (6, 10)], "collision")
    body["clips"][0]["id"] = str(uuid.uuid5(video.id, "collision:1"))
    response = await client.post(f"/api/v1/videos/{video.id}/clips/batch", json=body)
    assert response.status_code == 422, response.text
    active = (await client.get(f"/api/v1/videos/{video.id}/clips")).json()
    assert {c["id"] for c in active} == {str(c.id) for c in clips}


async def test_stale_attempt_and_wrong_recording_cannot_republish_batch_receipts(workspace):
    from datetime import UTC, datetime, timedelta

    from app.models import JobStatus, JobType, ProcessingJob

    client, db, session = workspace
    video, clips = await film(db, session, [(0, 5), (5, 10)])
    video.duration_seconds = 10
    for clip in clips:
        clip.boundary_source = "heuristic"
    current_token = uuid.uuid4()
    job = ProcessingJob(
        id=uuid.uuid4(),
        video_id=video.id,
        job_type=JobType.pipeline,
        status=JobStatus.running,
        leased_by="batch-test",
        lease_token=current_token,
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    db.add(job)
    await db.flush()
    body = generation(clips, [(0, 6), (6, 10)], "fenced")
    for proposal in body["clips"]:
        proposal["job_id"] = str(job.id)
    headers = {
        "X-Job-ID": str(job.id),
        "X-Worker-ID": "batch-test",
        "X-Job-Lease-Token": str(uuid.uuid4()),
    }
    stale = await client.post(f"/api/v1/videos/{video.id}/clips/batch", json=body, headers=headers)
    assert stale.status_code == 409, stale.text
    headers["X-Job-Lease-Token"] = str(current_token)
    accepted = await client.post(
        f"/api/v1/videos/{video.id}/clips/batch", json=body, headers=headers
    )
    assert accepted.status_code == 200, accepted.text
    other_video, _ = await film(db, session, [(0, 10)])
    job.video_id = other_video.id
    await db.flush()
    wrong_recording_retry = await client.post(
        f"/api/v1/videos/{video.id}/clips/batch", json=body, headers=headers
    )
    assert wrong_recording_retry.status_code == 409, wrong_recording_retry.text
    assert len((await client.get(f"/api/v1/videos/{video.id}/clips")).json()) == 2
