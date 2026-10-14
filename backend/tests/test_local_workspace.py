"""Real database checks for local workspace, queue fencing, and report bytes."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio
from app.config import get_settings
from app.database import Base
from app.main import app
from app.models import (
    Clip,
    FootballSession,
    JobStatus,
    JobType,
    Player,
    ProcessingJob,
    ReportFormat,
    ReportJob,
    Tracklet,
    User,
    UserRole,
    Video,
)
from app.reports import aggregate_coaching_summary
from app.routers.jobs import (
    JobClaimRequest,
    JobCreate,
    JobHeartbeatRequest,
    JobStatusUpdate,
    _control_job,
    claim_job,
    create_job,
    heartbeat_job,
    update_job_status,
)
from app.routers.reports import _run_report_job
from app.routers.sessions import PlayMatch, ensure_recording_plays, match_play
from app.routers.system import staff_storage_url
from fastapi import HTTPException, Response
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest_asyncio.fixture
async def workspace_db():
    schema = f"workspace_test_{uuid.uuid4().hex}"
    admin_engine = create_async_engine(get_settings().database_url)
    async with admin_engine.begin() as conn:
        await conn.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    engine = create_async_engine(
        get_settings().database_url,
        connect_args={"server_settings": {"search_path": f"{schema},public"}},
    )
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, checkfirst=False))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    suffix = uuid.uuid4().hex
    async with maker() as db:
        user = User(
            id=uuid.uuid4(),
            email=f"workspace-{suffix}@test.local",
            hashed_password="x",
            full_name="Workspace tester",
            role=UserRole.admin,
        )
        session = FootballSession(
            id=uuid.uuid4(), title=f"workspace-test-{suffix}", created_by=user.id
        )
        db.add(user)
        await db.flush()
        db.add(session)
        await db.commit()
    try:
        yield maker, user, session
    finally:
        async with maker() as db:
            await db.execute(
                delete(ProcessingJob).where(
                    ProcessingJob.video_id.in_(
                        select(Video.id).where(Video.filename.like(f"workspace-{suffix}%"))
                    )
                )
            )
            await db.execute(delete(Video).where(Video.filename.like(f"workspace-{suffix}%")))
            await db.execute(delete(FootballSession).where(FootballSession.id == session.id))
            await db.execute(delete(Player).where(Player.last_name == f"RosterTest{suffix}"))
            await db.execute(delete(User).where(User.id == user.id))
            await db.commit()
        await engine.dispose()
        async with admin_engine.begin() as conn:
            await conn.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        await admin_engine.dispose()


async def _recording(db, session, suffix, camera):
    video = Video(
        id=uuid.uuid4(),
        filename=f"workspace-{suffix}-{camera}.mp4",
        storage_uri=f"local://raw-video/{suffix}-{camera}.mp4",
        practice_session_id=session.id,
        camera_label=camera,
    )
    db.add(video)
    await db.flush()
    clip = Clip(
        id=uuid.uuid4(),
        video_id=video.id,
        start_time=0,
        end_time=5,
        is_reviewed=True,
        personnel_grouping="11",
    )
    db.add(clip)
    await db.flush()
    await ensure_recording_plays(db, video)
    return video, clip


async def test_matching_independent_views_counts_one_football_play(workspace_db):
    maker, user, session = workspace_db
    suffix = user.email.split("@")[0].removeprefix("workspace-")
    async with maker() as db:
        first, clip1 = await _recording(db, session, suffix, "sideline")
        second, clip2 = await _recording(db, session, suffix, "endzone")
        result = await match_play(
            session.id, PlayMatch(clip_ids=[clip1.id, clip2.id], title="First snap"), db, user
        )
        assert result["recording_count"] == 2
        assert result["play_count"] == 1
        assert {v["video_id"] for v in result["plays"][0]["views"]} == {
            str(first.id),
            str(second.id),
        }
        assert all(v["alignment_offset_seconds"] is None for v in result["plays"][0]["views"])
        report = await aggregate_coaching_summary(
            db, scope={"practice_session_id": str(session.id)}
        )
        assert report.totals["clips"] == 1
        assert report.totals["reviewed_clips"] == 1
        assert report.totals["videos"] == 2
        await db.commit()


async def test_concurrent_pipeline_enqueue_is_idempotent(workspace_db):
    maker, user, session = workspace_db
    suffix = user.email.split("@")[0].removeprefix("workspace-")
    async with maker() as db:
        video, _ = await _recording(db, session, suffix, "queue")
        await db.commit()

    async def enqueue():
        async with maker() as db:
            response = await create_job(
                JobCreate(video_id=video.id, job_type=JobType.pipeline, processing_profile="quick"),
                Response(),
                db,
                user,
                None,
            )
            await db.commit()
            return response.id

    first, second = await asyncio.gather(enqueue(), enqueue())
    assert first == second
    async with maker() as db:
        rows = (
            (await db.execute(select(ProcessingJob).where(ProcessingJob.video_id == video.id)))
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].input_artifacts["processing_profile"] == "quick"
        await db.delete(rows[0])
        await db.commit()


async def test_stale_worker_attempt_cannot_publish_and_pause_survives_resume(workspace_db):
    maker, user, session = workspace_db
    suffix = user.email.split("@")[0].removeprefix("workspace-")
    async with maker() as db:
        video, _ = await _recording(db, session, suffix, "lease")
        row = ProcessingJob(
            id=uuid.uuid4(),
            video_id=video.id,
            job_type=JobType.pipeline,
            status=JobStatus.queued,
            priority=100000,
        )
        db.add(row)
        await db.commit()
    async with maker() as db:
        first = await claim_job(JobClaimRequest(worker_id="workspace-first"), Response(), db, user)
        assert first.id == row.id
        await db.commit()
    async with maker() as db:
        current = await db.get(ProcessingJob, row.id)
        current.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()
    async with maker() as db:
        second = await claim_job(
            JobClaimRequest(worker_id="workspace-second"), Response(), db, user
        )
        assert second.id == row.id and second.lease_token != first.lease_token
        await db.commit()
    async with maker() as db:
        with pytest.raises(HTTPException) as stale:
            await update_job_status(
                row.id,
                JobStatusUpdate(
                    status=JobStatus.succeeded,
                    worker_id="workspace-first",
                    lease_token=first.lease_token,
                ),
                db,
                user,
            )
        assert stale.value.status_code == 409
        await db.rollback()
        pending = await _control_job(row.id, "pause", db)
        assert pending.status == "running" and pending.control_requested == "pause"
        await heartbeat_job(
            row.id,
            JobHeartbeatRequest(
                worker_id="workspace-second",
                lease_token=second.lease_token,
                progress={"ingest": {"status": "succeeded"}},
            ),
            db,
            user,
        )
        await update_job_status(
            row.id,
            JobStatusUpdate(
                status=JobStatus.paused,
                worker_id="workspace-second",
                lease_token=second.lease_token,
            ),
            db,
            user,
        )
        resumed = await _control_job(row.id, "resume", db)
        assert resumed.status == "queued"
        assert resumed.progress["ingest"]["status"] == "succeeded"
        await db.delete(await db.get(ProcessingJob, row.id))
        await db.commit()


async def test_persisted_report_generates_downloadable_local_bytes(
    workspace_db, monkeypatch, tmp_path
):
    maker, user, session = workspace_db
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("LOCAL_STORAGE_ROOT", str(tmp_path))
    get_settings.cache_clear()
    monkeypatch.setattr("app.routers.reports.AsyncSessionLocal", maker)
    async with maker() as db:
        job = ReportJob(
            id=uuid.uuid4(),
            report_type="coaching_summary",
            format=ReportFormat.json,
            requested_by=user.id,
            parameters={"scope": {"practice_session_id": str(session.id)}},
        )
        db.add(job)
        await db.commit()
    # Simulates the recovery loop finding a persisted queued request after restart.
    await _run_report_job(job.id)
    async with maker() as db:
        output = await db.get(ReportJob, job.id)
        assert output.status == JobStatus.succeeded
        assert output.attempt_count == 1
        url = staff_storage_url(output.output_uri)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as client:
            response = await client.get(url)
        assert response.status_code == 200
        assert response.json()["scope"]["practice_session_id"] == str(session.id)
        assert response.json()["totals"]["videos"] == 0
        await db.delete(output)
        await db.commit()


async def test_roster_import_replays_and_edits_keep_tracklet_identity(workspace_db):
    from app.routers.players import PlayerUpdate, RosterImport, import_roster, update_player

    maker, user, session = workspace_db
    suffix = user.email.split("@")[0].removeprefix("workspace-")
    csv_body = (
        "first_name,last_name,jersey_number,position,position_group\n"
        f"Alpha,RosterTest{suffix},98,QB,QB\n"
        f"Beta,RosterTest{suffix},98,QB,QB\n"
        f"Gamma,RosterTest{suffix},999,WR,Skill\n"
    )
    async with maker() as db:
        preview = await import_roster(RosterImport(csv=csv_body), db, user)
        assert preview["created"] == 1 and len(preview["errors"]) == 2
        imported = await import_roster(RosterImport(csv=csv_body, dry_run=False), db, user)
        assert imported["created"] == 1 and imported["updated"] == 0
        player_id = uuid.UUID(imported["rows"][0]["player"]["id"])
        replay = await import_roster(RosterImport(csv=csv_body, dry_run=False), db, user)
        assert replay["created"] == 0 and replay["skipped"] == 1
        _, clip = await _recording(db, session, suffix, "roster")
        tracklet = Tracklet(clip_id=clip.id, player_id=player_id, start_frame=0, end_frame=30)
        db.add(tracklet)
        await db.flush()
        edited = await update_player(player_id, PlayerUpdate(jersey_number=97), db, user)
        assert edited["id"] == str(player_id)
        assert tracklet.player_id == player_id
        from app.routers.tracklets import TrackletUpdate, update_tracklet

        confirmed = await update_tracklet(
            tracklet.id, TrackletUpdate(player_id=player_id, team_label="home"), db, user
        )
        assert confirmed.player_id == player_id
        replay = await update_tracklet(
            tracklet.id,
            TrackletUpdate(player_id=None, team_label="away"),
            db,
            user,
            worker_job_id="model-attempt",
        )
        assert replay.player_id == player_id and replay.team_label == "home"
        conflict = await import_roster(RosterImport(csv=csv_body, dry_run=False), db, user)
        assert conflict["rows"][0]["status"] == "conflict"
        assert (await db.get(Player, player_id)).jersey_number == 97
        await db.commit()


async def test_shutdown_recovery_preserves_explicit_pause_and_cancel(workspace_db):
    from app.services.job_sweeper import recover_shutdown_pauses

    maker, user, session = workspace_db
    suffix = user.email.split("@")[0].removeprefix("workspace-")
    async with maker() as db:
        video, _ = await _recording(db, session, suffix, "shutdown")
        shutdown = ProcessingJob(
            video_id=video.id,
            job_type=JobType.pipeline,
            status=JobStatus.paused,
            attempt_count=1,
            output_artifacts={"interrupted_action": "shutdown"},
        )
        explicit = ProcessingJob(
            video_id=video.id,
            job_type=JobType.detect,
            status=JobStatus.paused,
            attempt_count=1,
            output_artifacts={"interrupted_action": "pause"},
        )
        cancelled = ProcessingJob(
            video_id=video.id,
            job_type=JobType.render,
            status=JobStatus.running,
            leased_by="shutdown-worker",
            lease_token=uuid.uuid4(),
            lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
            control_requested="cancel",
        )
        db.add_all([shutdown, explicit, cancelled])
        await db.flush()
        response = await update_job_status(
            cancelled.id,
            JobStatusUpdate(
                status=JobStatus.paused,
                worker_id="shutdown-worker",
                lease_token=cancelled.lease_token,
                output_artifacts={"interrupted_action": "shutdown"},
            ),
            db,
            user,
        )
        assert response.status == "cancelled"
        assert await recover_shutdown_pauses(db) == 1
        await db.refresh(shutdown)
        await db.refresh(explicit)
        assert shutdown.status == JobStatus.queued and shutdown.attempt_count == 0
        assert explicit.status == JobStatus.paused
        await db.commit()
