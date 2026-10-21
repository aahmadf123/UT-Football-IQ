"""Real PostgreSQL contracts for the connected overhaul workflow."""

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, date, datetime, timedelta

import pytest
from app.config import get_settings
from app.database import get_db
from app.deps import get_current_user
from app.main import app
from app.models import (
    Clip,
    Event,
    FootballSession,
    JobStatus,
    JobType,
    Label,
    Metric,
    Play,
    Player,
    PlayView,
    ProcessingJob,
    ReportJob,
    RosterAssignment,
    SessionKind,
    SystemSetting,
    Tracklet,
    TrackPoint,
    User,
    UserRole,
    Video,
    VideoStatus,
)
from app.routers.analytics import AnalyticsQuery, query_analytics, survival_curve
from app.routers.events import EventCreate, create_event, effective_events
from app.routers.play_evidence import play_evidence
from app.routers.roster_assignments import roster_for_video
from app.routers.videos import _maybe_auto_enqueue_pipeline
from app.services.metric_eligibility import automatic_exclusion
from app.services.metric_policy import aggregate_metric_ids, metric_visible
from app.services.processing_intent import schedule_detailed_followup
from app.services.publication import (
    publish_job_clip_tiers,
    publish_job_events,
    publish_job_metrics,
    publish_job_tracks,
)
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


@pytest.fixture
async def db() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine(get_settings().database_url)
    async with engine.connect() as connection:
        transaction = await connection.begin()
        async with AsyncSession(bind=connection, expire_on_commit=False) as session:
            yield session
        await transaction.rollback()
    await engine.dispose()


async def actor(db: AsyncSession, name: str = "Owner") -> User:
    user = User(
        id=uuid.uuid4(),
        email=f"overhaul-{uuid.uuid4().hex}@example.invalid",
        hashed_password="unused-test-hash",
        full_name=name,
        role=UserRole.analyst,
    )
    db.add(user)
    await db.flush()
    return user


async def recording(db: AsyncSession) -> tuple[Video, Clip]:
    video = Video(
        id=uuid.uuid4(),
        filename=f"overhaul-{uuid.uuid4()}.mp4",
        storage_uri=f"local://raw-video/{uuid.uuid4()}.mp4",
        status=VideoStatus.ready,
        fps=30,
        duration_seconds=30,
        metadata_={"source": "real"},
    )
    db.add(video)
    await db.flush()
    clip = Clip(
        id=uuid.uuid4(), video_id=video.id, start_time=0, end_time=5, provenance={"source": "real"}
    )
    db.add(clip)
    await db.flush()
    return video, clip


async def player_track(
    db: AsyncSession, clip: Clip, player: Player | None = None, job: ProcessingJob | None = None
) -> Tracklet:
    track = Tracklet(
        id=uuid.uuid4(),
        clip_id=clip.id,
        player_id=player.id if player else None,
        start_frame=0,
        end_frame=30,
        job_id=job.id if job else None,
        provenance={"source": "real", **({"publication_state": "pending"} if job else {})},
    )
    db.add(track)
    await db.flush()
    return track


def measurement(clip: Clip, track: Tracklet, value: float, **kwargs: object) -> Metric:
    return Metric(
        id=uuid.uuid4(),
        clip_id=clip.id,
        tracklet_id=track.id,
        metric_name="distance_traveled",
        metric_value={"yards": value},
        unit="yd",
        analytics_safe=True,
        experimental_flag=False,
        is_suppressed=False,
        provenance={"source": "real", "validated": True, "definition_version": "v1"},
        **kwargs,
    )


async def test_upload_intent_survives_saturation_and_replay(db: AsyncSession) -> None:
    video, _clip = await recording(db)
    for _ in range(60):
        db.add(ProcessingJob(job_type=JobType.pipeline, status=JobStatus.queued))
    await db.flush()
    first = await _maybe_auto_enqueue_pipeline(db, video)
    second = await _maybe_auto_enqueue_pipeline(db, video)
    assert first is not None and second is not None and first.id == second.id
    rows = (
        (await db.execute(select(ProcessingJob).where(ProcessingJob.video_id == video.id)))
        .scalars()
        .all()
    )
    assert len(rows) == 1 and rows[0].status == JobStatus.queued
    assert first.priority == 10 and first.input_artifacts["processing_profile"] == "quick"
    first.status = JobStatus.succeeded
    detailed = await schedule_detailed_followup(db, first)
    replay = await schedule_detailed_followup(db, first)
    assert detailed is not None and replay is not None and detailed.id == replay.id
    assert detailed.priority == 0 and detailed.status == JobStatus.queued
    assert detailed.input_artifacts["processing_profile"] == "detailed"
    detailed.status = JobStatus.succeeded
    assert await schedule_detailed_followup(db, detailed) is None


@pytest.mark.parametrize("profile", ["review", "detailed", "quick"])
async def test_explicit_profile_and_disabled_detail_intent_do_not_create_followup(
    db: AsyncSession,
    profile: str,
) -> None:
    config = await db.get(SystemSetting, "system_config")
    if config is None:
        config = SystemSetting(key="system_config", value={})
        db.add(config)
    config.value = {
        "auto_process_on_upload": True,
        "auto_processing_profile": profile,
        "auto_detailed_analysis": False,
    }
    video, _clip = await recording(db)
    job = await _maybe_auto_enqueue_pipeline(db, video)
    assert job is not None and job.input_artifacts["processing_profile"] == profile
    job.status = JobStatus.succeeded
    assert await schedule_detailed_followup(db, job) is None


async def test_canonical_analytics_reconciles_views_fragments_unknowns(db: AsyncSession) -> None:
    user = await actor(db)
    video, clip = await recording(db)
    alternate = Clip(id=uuid.uuid4(), video_id=video.id, start_time=10, end_time=15)
    person = Player(id=uuid.uuid4(), first_name="One", last_name=f"Overhaul{uuid.uuid4()}")
    session = FootballSession(id=uuid.uuid4(), title="Canonical test", library_role="toledo")
    db.add_all([alternate, person, session])
    await db.flush()
    play = Play(id=uuid.uuid4(), session_id=session.id)
    db.add(play)
    await db.flush()
    db.add_all(
        [
            PlayView(play_id=play.id, clip_id=clip.id),
            PlayView(play_id=play.id, clip_id=alternate.id),
        ]
    )
    tracks = [
        await player_track(db, clip, person),
        await player_track(db, alternate, person),
        await player_track(db, alternate),
    ]
    tracks[0].track_confidence, tracks[1].track_confidence = 0.9, 0.4
    good = measurement(clip, tracks[0], 12, confidence=0.9)
    other = measurement(alternate, tracks[1], 50, confidence=0.4)
    unknown = measurement(alternate, tracks[2], 99)
    db.add_all([good, other, unknown])
    await db.flush()
    out = await query_analytics(
        AnalyticsQuery(metric_names=["distance_traveled"], session_ids=[]), db, user
    )
    groups = [
        group
        for group in out["groups"]
        if any(item["metric_id"] == str(good.id) for item in group["evidence"])
    ]
    assert len(groups) == 1 and groups[0]["n"] == 1 and groups[0]["mean"] == 12
    assert groups[0]["evidence"][0]["play_id"] == str(play.id)
    assert any(item["reason"] == "unresolved_player" for item in out["exclusions"])
    selected = set((await db.execute(aggregate_metric_ids())).scalars().all())
    assert good.id in selected and other.id not in selected and unknown.id not in selected


async def test_publication_is_atomic_and_preserves_human_override(db: AsyncSession) -> None:
    user = await actor(db)
    video, clip = await recording(db)
    person = Player(id=uuid.uuid4(), first_name="Two", last_name=f"Overhaul{uuid.uuid4()}")
    old_job = ProcessingJob(
        id=uuid.uuid4(), video_id=video.id, job_type=JobType.pipeline, status=JobStatus.succeeded
    )
    new_job = ProcessingJob(
        id=uuid.uuid4(), video_id=video.id, job_type=JobType.pipeline, status=JobStatus.running
    )
    db.add_all([person, old_job, new_job])
    await db.flush()
    old_track = await player_track(db, clip, person)
    old_track.job_id = old_job.id
    new_track = await player_track(db, clip, person, new_job)
    old = measurement(clip, old_track, 10, job_id=old_job.id)
    manual = measurement(clip, old_track, 11)
    manual.provenance = {"source": "human"}
    candidate = measurement(clip, new_track, 13, job_id=new_job.id)
    candidate.provenance = {"source": "real", "validated": True, "publication_state": "pending"}
    old_specialist = measurement(clip, old_track, 17, job_id=old_job.id)
    old_specialist.metric_name = "route_depth"
    db.add_all([old, manual, candidate, old_specialist])
    await db.flush()
    assert metric_visible(old, user) and not metric_visible(candidate, user)
    await publish_job_metrics(db, new_job.id)
    await publish_job_tracks(db, new_job.id)
    await db.flush()
    assert metric_visible(candidate, user)
    assert old.is_suppressed and old_specialist.is_suppressed
    assert not manual.is_suppressed and metric_visible(manual, user)
    assert old_track.provenance["state"] == "superseded"
    assert "publication_state" not in new_track.provenance


async def test_saved_views_authorization_validation_and_team_scope(db: AsyncSession) -> None:
    owner, colleague = await actor(db), await actor(db, "Colleague")
    current = [owner]

    async def override_db() -> AsyncGenerator[AsyncSession, None]:
        yield db

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = lambda: current[0]
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            private = await client.post(
                "/api/v1/saved-views", json={"name": "Private", "surface": "analytics"}
            )
            shared = await client.post(
                "/api/v1/saved-views",
                json={"name": "Team", "surface": "analytics", "visibility": "team"},
            )
            assert private.status_code == shared.status_code == 201
            view_id = shared.json()["id"]
            invalid = await client.patch(f"/api/v1/saved-views/{view_id}", json={"name": "   "})
            assert invalid.status_code == 422
            current[0] = colleague
            listed = (await client.get("/api/v1/saved-views")).json()
            assert shared.json()["id"] in {row["id"] for row in listed}
            assert private.json()["id"] not in {row["id"] for row in listed}
            assert next(row for row in listed if row["id"] == view_id)["can_edit"] is False
            assert (await client.delete(f"/api/v1/saved-views/{view_id}")).status_code == 404
    finally:
        app.dependency_overrides.clear()


async def test_observation_shot_visibility_survives_api_roundtrip(db: AsyncSession) -> None:
    user = await actor(db)
    _video, clip = await recording(db)

    async def override_db() -> AsyncGenerator[AsyncSession, None]:
        yield db

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/api/v1/tracklets",
                json={
                    "clip_id": str(clip.id),
                    "start_frame": 0,
                    "end_frame": 3,
                    "provenance": {"source": "real"},
                    "track_points": [
                        {
                            "frame_number": 0,
                            "timestamp_seconds": 0,
                            "shot_id": 1,
                            "visibility_state": "visible",
                            "bbox": [1, 2, 3, 4],
                        },
                        {
                            "frame_number": 3,
                            "timestamp_seconds": 0.1,
                            "shot_id": 2,
                            "visibility_state": "lost",
                        },
                    ],
                },
            )
            assert response.status_code == 201, response.text
            track = (await client.get(f"/api/v1/tracklets/{response.json()['id']}")).json()
            assert track["track_points"][0]["shot_id"] == "1"
            overlay = await client.get(f"/api/v1/clips/{clip.id}/overlays")
            assert overlay.status_code == 200, overlay.text
            points = overlay.json()["tracklets"][0]["track_points"]
            assert points[1]["visibility_state"] == "lost" and points[1]["shot_id"] == "2"
    finally:
        app.dependency_overrides.clear()


async def test_report_is_durable_before_background_and_immediate_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine(get_settings().database_url)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        user = await actor(session, "Report owner")
        await session.commit()
    observed: list[bool] = []
    report_id: uuid.UUID | None = None

    async def independent_background(job_id: uuid.UUID) -> None:
        async with maker() as session:
            observed.append(await session.get(ReportJob, job_id) is not None)

    async def override_db() -> AsyncGenerator[AsyncSession, None]:
        async with maker() as session:
            yield session
            await session.commit()

    monkeypatch.setattr("app.routers.reports._run_report_job", independent_background)
    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            created = await client.post("/api/v1/reports", json={"format": "json"})
            assert created.status_code == 201, created.text
            report_id = uuid.UUID(created.json()["id"])
            immediate = await client.get(f"/api/v1/reports/{report_id}")
            assert immediate.status_code == 200 and observed == [True]
    finally:
        app.dependency_overrides.clear()
        async with maker() as session:
            if report_id:
                row = await session.get(ReportJob, report_id)
                if row:
                    await session.delete(row)
            row = await session.get(User, user.id)
            if row:
                await session.delete(row)
            await session.commit()
        await engine.dispose()


@pytest.mark.parametrize("identical", [True, False])
async def test_identity_override_survives_replacement_or_preserves_complete_old_set(
    db: AsyncSession,
    identical: bool,
) -> None:
    user = await actor(db)
    video, clip = await recording(db)
    person = Player(id=uuid.uuid4(), first_name="Corrected", last_name=f"Overhaul{uuid.uuid4()}")
    old_job = ProcessingJob(
        id=uuid.uuid4(), video_id=video.id, job_type=JobType.pipeline, status=JobStatus.succeeded
    )
    new_job = ProcessingJob(
        id=uuid.uuid4(), video_id=video.id, job_type=JobType.pipeline, status=JobStatus.succeeded
    )
    db.add_all([person, old_job, new_job])
    await db.flush()
    old = await player_track(db, clip, person)
    old.job_id = old_job.id
    old.team_label = "home"
    new = await player_track(db, clip, job=new_job)
    db.add(
        Label(
            clip_id=clip.id,
            tracklet_id=old.id,
            label_type="player_identity",
            source="human",
            annotated_by=user.id,
            label_value={"player_id": str(person.id), "team_label": "home"},
        )
    )
    for track in (old, new):
        for frame in range(3):
            shift = 100 if track is new and not identical else 0
            db.add(
                TrackPoint(
                    tracklet_id=track.id,
                    frame_number=frame,
                    timestamp_seconds=frame / 30,
                    bbox=[10 + shift, 10, 20 + shift, 30],
                    shot_id="1",
                    visibility_state="visible",
                )
            )
    current_metric = measurement(clip, old, 10, job_id=old_job.id)
    pending_metric = measurement(clip, new, 12, job_id=new_job.id)
    pending_metric.provenance = {"source": "real", "publication_state": "pending"}
    db.add_all([current_metric, pending_metric])
    await db.flush()
    await publish_job_tracks(db, new_job.id)
    await publish_job_metrics(db, new_job.id)
    await db.flush()
    if identical:
        assert new.player_id == person.id and new.team_label == "home"
        assert new.provenance["identity_override"]["source_tracklet_id"] == str(old.id)
        assert old.provenance["state"] == "superseded"
        inherited = (
            await db.execute(select(Label).where(Label.tracklet_id == new.id))
        ).scalar_one()
        assert inherited.source == "inherited_human"
        assert metric_visible(pending_metric, user) and current_metric.is_suppressed
    else:
        assert old.provenance.get("state") != "superseded"
        assert new.provenance["exclusion_reason"] == "correction_rebinding_unresolved"
        assert new.provenance["publication_state"] == "pending"
        assert metric_visible(current_metric, user) and not metric_visible(pending_metric, user)


def test_censored_durations_leave_risk_set_and_do_not_become_zeroes() -> None:
    evidence = [
        {"value": 1.0, "right_censored": False},
        {"value": 2.0, "right_censored": True},
        {"value": 3.0, "right_censored": False},
    ]
    curve = survival_curve(evidence)
    assert curve[1]["survival"] == pytest.approx(2 / 3)
    assert curve[2]["censored"] == 1 and curve[2]["survival"] == pytest.approx(2 / 3)
    assert curve[3]["at_risk"] == 1 and curve[3]["survival"] == 0


async def test_worker_validated_flag_cannot_self_approve_a_model(db: AsyncSession) -> None:
    user = await actor(db)
    _video, clip = await recording(db)

    async def override_db() -> AsyncGenerator[AsyncSession, None]:
        yield db

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/api/v1/metrics",
                json={
                    "clip_id": str(clip.id),
                    "metric_name": "route_depth",
                    "metric_value": {"yards": 12},
                    "analytics_safe": True,
                    "provenance": {"source": "real", "validated": True},
                },
            )
            assert response.status_code == 201, response.text
            assert response.json()["analytics_safe"] is False
            assert response.json()["provenance"]["exclusion_reason"] == "model_not_approved"
    finally:
        app.dependency_overrides.clear()


async def test_roster_candidates_apply_season_dates_and_session_overrides(db: AsyncSession) -> None:
    user = await actor(db)
    video, _clip = await recording(db)
    session = FootballSession(
        id=uuid.uuid4(), title="Scoped practice", session_kind=SessionKind.practice
    )
    first = Player(
        id=uuid.uuid4(),
        first_name="First",
        last_name=f"Scope{uuid.uuid4()}",
        jersey_number=7,
        position_group="QB",
    )
    second = Player(
        id=uuid.uuid4(),
        first_name="Second",
        last_name=f"Scope{uuid.uuid4()}",
        jersey_number=7,
        position_group="QB",
    )
    opponent = Player(
        id=uuid.uuid4(), first_name="Opponent", last_name=f"Scope{uuid.uuid4()}", jersey_number=7
    )
    db.add_all([session, first, second, opponent])
    await db.flush()
    video.practice_session_id = session.id
    video.session_kind = SessionKind.practice
    video.recorded_at = datetime(2026, 10, 21, tzinfo=UTC)
    db.add_all(
        [
            RosterAssignment(
                player_id=first.id, team="Toledo", season=2026, unit="offense", jersey_number=7
            ),
            RosterAssignment(
                player_id=first.id,
                team="Toledo",
                season=2026,
                unit="offense",
                jersey_number=8,
                session_id=session.id,
            ),
            RosterAssignment(
                player_id=second.id, team="Toledo", season=2026, unit="offense", jersey_number=7
            ),
            RosterAssignment(
                player_id=opponent.id, team="Buffalo", season=2026, unit="offense", jersey_number=7
            ),
            RosterAssignment(
                player_id=first.id,
                team="Toledo",
                season=2026,
                unit="defense",
                jersey_number=90,
                effective_until=date(2026, 9, 1),
            ),
            RosterAssignment(
                player_id=second.id, team="Toledo", season=2025, unit="offense", jersey_number=80
            ),
        ]
    )
    await db.flush()
    result = await roster_for_video(video.id, db, user)
    assert [(row["id"], row["jersey_number"]) for row in result if row["id"] == str(first.id)] == [
        (str(first.id), 8)
    ]
    assert any(row["id"] == str(second.id) and row["jersey_number"] == 7 for row in result)
    assert not any(row["id"] == str(opponent.id) for row in result)


async def test_distinct_gap_subjects_survive_alternate_view_deduplication(db: AsyncSession) -> None:
    user = await actor(db)
    video, clip = await recording(db)
    session = FootballSession(id=uuid.uuid4(), title="Gap subjects")
    db.add(session)
    await db.flush()
    video.practice_session_id = session.id
    alternate = Clip(id=uuid.uuid4(), video_id=video.id, start_time=10, end_time=15)
    play = Play(id=uuid.uuid4(), session_id=session.id)
    db.add_all([alternate, play])
    await db.flush()
    db.add_all(
        [
            PlayView(play_id=play.id, clip_id=clip.id),
            PlayView(play_id=play.id, clip_id=alternate.id),
        ]
    )
    ids = []
    for source, confidence in ((clip, 0.9), (alternate, 0.4)):
        for gap in range(4):
            metric = Metric(
                id=uuid.uuid4(),
                clip_id=source.id,
                metric_name="geometric_spacing",
                metric_value={
                    "width_yards": gap + 1.0,
                    "gap_index": gap,
                    "initial_pair": [gap, gap + 1],
                },
                confidence=confidence,
                unit="yd",
                analytics_safe=True,
                provenance={"source": "real", "validated": True},
            )
            db.add(metric)
            ids.append(metric.id)
    await db.flush()
    out = await query_analytics(
        AnalyticsQuery(metric_names=["geometric_spacing"], session_ids=[session.id]), db, user
    )
    assert out["cohort"]["plays"] == 1 and out["cohort"]["eligible_measurements"] == 4
    assert len(out["groups"]) == 4 and all(group["n"] == 1 for group in out["groups"])
    assert {group["evidence"][0]["subject"] for group in out["groups"]} == {
        f"gap:{i}" for i in range(4)
    }
    selected = set((await db.execute(aggregate_metric_ids())).scalars().all())
    assert len(selected.intersection(ids)) == 4


async def test_play_readiness_ignores_pending_and_historical_tracks(db: AsyncSession) -> None:
    user = await actor(db)
    video, clip = await recording(db)
    person = Player(id=uuid.uuid4(), first_name="Current", last_name=f"Readiness{uuid.uuid4()}")
    db.add(person)
    await db.flush()
    current = await player_track(db, clip)
    old = await player_track(db, clip, person)
    old.provenance = {"source": "real", "state": "superseded"}
    pending = await player_track(db, clip, person)
    pending.provenance = {"source": "real", "publication_state": "pending"}
    labels = [
        Label(
            clip_id=clip.id,
            tracklet_id=track.id if track else None,
            label_type="route_family",
            source="human",
            annotated_by=user.id,
            label_value={"route": "go"},
        )
        for track in (current, old, pending, None)
    ]
    db.add_all(labels)
    unverified = [
        Label(
            clip_id=clip.id,
            tracklet_id=current.id,
            label_type="coverage_shell",
            source="model",
            label_value={"coverage": "legacy", "validated": True},
        ),
        Label(
            clip_id=clip.id,
            tracklet_id=current.id,
            label_type="defensive_front",
            source="human",
            label_value={"front": "missing-annotator"},
        ),
    ]
    db.add_all(unverified)
    await db.flush()
    out = await play_evidence(clip.id, db, user)
    assert out["readiness"]["tracking"] is True
    assert out["readiness"]["identity_coverage"] == 0
    assert out["readiness"]["historical_or_pending_tracks"] == 2
    assert [row["tracklet_id"] for row in out["identities"]] == [str(current.id)]
    from app.routers.overlays import get_clip_overlays

    overlay = await get_clip_overlays(clip.id, db, user)
    assert {item.id for item in overlay.labels} == {labels[0].id, labels[3].id}
    assert all(item.qualification == "human_reviewed" for item in overlay.labels)
    assert not ({label.id for label in unverified} & {item.id for item in overlay.labels})
    assert overlay.withheld_label_count == 2


async def test_human_ttt_requires_current_forward_release_and_matching_timebase(
    db: AsyncSession,
) -> None:
    _video, clip = await recording(db)
    clip.start_time, clip.end_time = 100, 105
    obsolete = [
        Event(
            id=uuid.uuid4(),
            clip_id=clip.id,
            event_type=kind,
            timestamp_seconds=time,
            provenance={"source": "human", "state": "superseded"},
        )
        for kind, time in [("snap", 0), ("throw", 2)]
    ]
    snap = Event(
        id=uuid.uuid4(),
        clip_id=clip.id,
        event_type="snap",
        timestamp_seconds=1,
        provenance={"source": "human"},
        attributes={"timebase": "clip", "shot_id": "observed-shot"},
    )
    release = Event(
        id=uuid.uuid4(),
        clip_id=clip.id,
        event_type="release",
        timestamp_seconds=104,
        provenance={"source": "human"},
        attributes={"timebase": "recording", "shot_id": "observed-shot"},
    )
    db.add_all([*obsolete, snap, release])
    await db.flush()

    async def reason(seconds: float, censored: bool = False) -> str | None:
        return await automatic_exclusion(
            db,
            clip=clip,
            name="time_to_throw",
            value={"seconds": seconds, "right_censored": censored},
            provenance={"validation_source": "human_event_timestamps"},
            model_id=None,
        )

    assert await reason(2) == "required_event_evidence_missing"
    assert await reason(3) == "required_event_evidence_missing"
    release.attributes = {**release.attributes, "forward_pass": True}
    await db.flush()
    assert await reason(3) is None
    assert await reason(3, True) == "forward_pass_release_not_observed"
    release.provenance = {"source": "human", "publication_state": "pending"}
    await db.flush()
    assert await reason(3) == "required_event_evidence_missing"


async def test_event_run_publication_is_atomic_and_preserves_human(db: AsyncSession) -> None:
    user = await actor(db)
    _video, clip = await recording(db)
    old_job, new_job = uuid.uuid4(), uuid.uuid4()
    old = Event(
        id=uuid.uuid4(),
        clip_id=clip.id,
        event_type="snap",
        timestamp_seconds=1,
        provenance={"source": "real", "job_id": str(old_job)},
    )
    human = Event(
        id=uuid.uuid4(),
        clip_id=clip.id,
        event_type="throw",
        timestamp_seconds=3,
        provenance={"source": "human"},
    )
    db.add_all([old, human])
    await db.flush()
    response = await create_event(
        EventCreate(
            clip_id=clip.id,
            event_type="snap",
            timestamp_seconds=1.1,
            job_id=new_job,
            provenance={"source": "real"},
        ),
        db,
        user,
    )
    new = await db.get(Event, response.id)
    assert new is not None
    assert {e.id for e in effective_events([old, human, new])} == {old.id, human.id}
    await publish_job_events(db, new_job)
    assert {e.id for e in effective_events([old, human, new])} == {new.id, human.id}
    assert human.provenance == {"source": "human"}


@pytest.mark.parametrize(
    "position,group",
    [
        ("RB", "RB"),
        ("FB", "RB"),
        ("HB", "RB"),
        ("WR", "WR"),
        ("SLOT", "WR"),
        ("TE", "TE"),
        ("ATH", "Skill"),
    ],
)
def test_distinct_skill_groups_and_legacy_compatibility(position: str, group: str) -> None:
    from app.cfbd.roster_sync import position_group
    from app.routers.players import PlayerCreate

    assert position_group(position) == group
    assert (
        PlayerCreate(first_name="Test", last_name="Athlete", position_group=group).position_group
        == group
    )


async def test_cross_job_output_ids_cannot_mutate_prior_result_versions(db: AsyncSession) -> None:
    from app.routers.metrics import MetricCreate, create_metric
    from app.routers.tracklets import TrackletCreate, create_tracklet
    from fastapi import HTTPException

    user = await actor(db)
    _video, clip = await recording(db)
    track = await player_track(db, clip)
    metric = measurement(clip, track, 4)
    metric.provenance = {"source": "human"}
    event = Event(
        id=uuid.uuid4(),
        clip_id=clip.id,
        event_type="snap",
        timestamp_seconds=1,
        provenance={"source": "human"},
    )
    job = ProcessingJob(id=uuid.uuid4(), job_type=JobType.pipeline)
    db.add_all([metric, event, job])
    await db.flush()
    requests = [
        lambda: create_tracklet(
            TrackletCreate(
                id=track.id,
                clip_id=clip.id,
                start_frame=0,
                end_frame=30,
                job_id=job.id,
                provenance={"source": "real"},
            ),
            db,
            user,
        ),
        lambda: create_metric(
            MetricCreate(
                id=metric.id,
                clip_id=clip.id,
                tracklet_id=track.id,
                metric_name=metric.metric_name,
                metric_value={"yards": 999},
                job_id=job.id,
            ),
            db,
            user,
        ),
        lambda: create_event(
            EventCreate(
                id=event.id, clip_id=clip.id, event_type="snap", timestamp_seconds=2, job_id=job.id
            ),
            db,
            user,
        ),
    ]
    for request in requests:
        with pytest.raises(HTTPException) as caught:
            await request()
        assert caught.value.status_code == 409
    assert track.job_id is metric.job_id is None
    assert metric.metric_value == {"yards": 4} and event.timestamp_seconds == 1


@pytest.mark.parametrize("qualified_players", [0, 1, 2, 3])
async def test_complete_replacement_retains_fast_if_any_actor_unqualified(
    db: AsyncSession, qualified_players: int
) -> None:
    user = await actor(db)
    video, clip = await recording(db)
    clip.result_state = "preliminary"
    jobs = [
        ProcessingJob(id=uuid.uuid4(), video_id=video.id, job_type=JobType.pipeline)
        for _ in range(2)
    ]
    players = [
        Player(id=uuid.uuid4(), first_name=f"Actor{i}", last_name=f"Publication{uuid.uuid4()}")
        for i in range(3)
    ]
    db.add_all([*jobs, *players])
    await db.flush()
    old_tracks, new_tracks, old_metrics, new_metrics = [], [], [], []
    for index, player in enumerate(players):
        old = await player_track(db, clip, player)
        old.job_id = jobs[0].id
        new = await player_track(db, clip, player, jobs[1])
        old_tracks.append(old)
        new_tracks.append(new)
        old_metric = measurement(clip, old, index + 3, job_id=jobs[0].id)
        if index == 2:
            old_metric.provenance = {"source": "human"}
        else:
            candidate = measurement(clip, new, index + 4, job_id=jobs[1].id)
            candidate.analytics_safe = index < qualified_players
            candidate.provenance = {"source": "real", "publication_state": "pending"}
            new_metrics.append(candidate)
            db.add(candidate)
        old_metrics.append(old_metric)
        db.add(old_metric)
    for job, safe, pending in ((jobs[0], True, False), (jobs[1], qualified_players == 2, True)):
        team_metric = Metric(
            id=uuid.uuid4(),
            clip_id=clip.id,
            job_id=job.id,
            metric_name="pre_snap_split",
            metric_value={"width_yards": 2},
            unit="yd",
            analytics_safe=safe,
            provenance={"source": "real", **({"publication_state": "pending"} if pending else {})},
        )
        (new_metrics if pending else old_metrics).append(team_metric)
        db.add(team_metric)
    old_event = Event(
        clip_id=clip.id,
        event_type="snap",
        timestamp_seconds=1,
        provenance={"source": "real", "job_id": str(jobs[0].id)},
    )
    new_event = Event(
        clip_id=clip.id,
        event_type="snap",
        timestamp_seconds=1.1,
        provenance={"source": "real", "job_id": str(jobs[1].id), "publication_state": "pending"},
    )
    db.add_all([old_event, new_event])
    await db.flush()
    from app.routers.clips import finalize_video_clips

    cuts = await finalize_video_clips(video.id, db, user)
    assert cuts.finalized_count == 0 and cuts.cuts_ready_count == 1
    assert clip.result_state == "preliminary"
    await publish_job_tracks(db, jobs[1].id)
    await publish_job_events(db, jobs[1].id)
    await publish_job_metrics(db, jobs[1].id)
    await publish_job_clip_tiers(db, jobs[1])
    await db.flush()
    selected = set((await db.execute(aggregate_metric_ids())).scalars().all())
    if qualified_players != 2:
        assert clip.result_state == "preliminary"
        assert all(track.provenance.get("state") != "superseded" for track in old_tracks)
        assert all(track.provenance.get("publication_state") == "pending" for track in new_tracks)
        assert all(
            track.provenance["exclusion_reason"] == "replacement_not_qualified"
            for track in new_tracks
        )
        assert {metric.id for metric in old_metrics}.issubset(selected)
        assert not any(metric.id in selected for metric in new_metrics)
        assert [event.id for event in effective_events([old_event, new_event])] == [old_event.id]
        readiness = await play_evidence(clip.id, db, user)
        assert {item["tracklet_id"] for item in readiness["identities"]} == {
            str(t.id) for t in old_tracks
        }
        from app.routers.overlays import get_clip_overlays

        overlays = await get_clip_overlays(clip.id, db, user)
        assert {track.id for track in overlays.tracklets} == {track.id for track in old_tracks}
        assert {metric.id for metric in overlays.metrics} == {metric.id for metric in old_metrics}
    else:
        assert clip.result_state == "final"
        assert all(track.provenance.get("state") == "superseded" for track in old_tracks)
        assert all(track.provenance.get("publication_state") != "pending" for track in new_tracks)
        assert {metric.id for metric in new_metrics}.issubset(selected)
        assert old_metrics[2].id in selected and not old_metrics[2].is_suppressed
        assert [event.id for event in effective_events([old_event, new_event])] == [new_event.id]
        from app.models import CorrectionType
        from app.services.annotations import submit_correction
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as caught:
            await submit_correction(
                db,
                actor=user,
                clip_id=clip.id,
                correction_type=CorrectionType.player_identity,
                corrected_value={
                    "tracklet_id": str(old_tracks[0].id),
                    "player_id": str(players[0].id),
                },
            )
        assert caught.value.status_code == 409


async def test_unqualified_first_pass_does_not_block_new_real_tracking(db: AsyncSession) -> None:
    video, clip = await recording(db)
    person = Player(id=uuid.uuid4(), first_name="First", last_name=f"Pass{uuid.uuid4()}")
    job = ProcessingJob(id=uuid.uuid4(), video_id=video.id, job_type=JobType.pipeline)
    db.add_all([person, job])
    await db.flush()
    old = await player_track(db, clip, person)
    new = await player_track(db, clip, person, job)
    metric = measurement(clip, old, 4)
    metric.analytics_safe = False
    db.add(metric)
    await db.flush()
    assert await publish_job_tracks(db, job.id) == 1
    assert old.provenance["state"] == "superseded"
    assert new.provenance.get("publication_state") != "pending"


@pytest.mark.parametrize("team_only", [False, True])
async def test_zero_track_replacement_preserves_previous_complete_result(
    db: AsyncSession, team_only: bool
) -> None:
    user = await actor(db)
    video, clip = await recording(db)
    clip.result_state = "preliminary"
    job = ProcessingJob(id=uuid.uuid4(), video_id=video.id, job_type=JobType.pipeline)
    person = Player(id=uuid.uuid4(), first_name="Zero", last_name=f"Tracks{uuid.uuid4()}")
    db.add_all([job, person])
    await db.flush()
    track = None if team_only else await player_track(db, clip, person)
    old = Metric(
        id=uuid.uuid4(),
        clip_id=clip.id,
        tracklet_id=track.id if track else None,
        metric_name="pre_snap_split" if team_only else "distance_traveled",
        metric_value={"width_yards": 2} if team_only else {"yards": 4},
        analytics_safe=True,
        provenance={"source": "real"},
    )
    new = Metric(
        id=uuid.uuid4(),
        clip_id=clip.id,
        job_id=job.id,
        metric_name=old.metric_name,
        metric_value=old.metric_value,
        analytics_safe=False,
        provenance={"source": "real", "publication_state": "pending"},
    )
    event = Event(
        id=uuid.uuid4(),
        clip_id=clip.id,
        event_type="snap",
        timestamp_seconds=1,
        provenance={"source": "real", "job_id": str(job.id), "publication_state": "pending"},
    )
    db.add_all([old, new, event])
    await db.flush()
    assert await publish_job_tracks(db, job.id) == 0
    await publish_job_events(db, job.id)
    await publish_job_metrics(db, job.id)
    await publish_job_clip_tiers(db, job)
    await db.flush()
    assert job.output_artifacts["publication_holds"][str(clip.id)] == "replacement_not_qualified"
    assert event.provenance["publication_state"] == new.provenance["publication_state"] == "pending"
    assert not effective_events([event])
    assert old.id in set((await db.execute(aggregate_metric_ids())).scalars().all())
    assert metric_visible(old, user) and not metric_visible(new, user)
    assert clip.result_state == "preliminary"


async def test_repeated_held_publication_reuses_inherited_identity_label(db: AsyncSession) -> None:
    user = await actor(db)
    video, clip = await recording(db)
    person = Player(id=uuid.uuid4(), first_name="Repeat", last_name=f"Held{uuid.uuid4()}")
    job = ProcessingJob(id=uuid.uuid4(), video_id=video.id, job_type=JobType.pipeline)
    db.add_all([person, job])
    await db.flush()
    old = await player_track(db, clip, person)
    new = await player_track(db, clip, job=job)
    label = Label(
        id=uuid.uuid4(),
        clip_id=clip.id,
        tracklet_id=old.id,
        label_type="player_identity",
        source="human",
        annotated_by=user.id,
        label_value={"player_id": str(person.id)},
    )
    db.add(label)
    for track in (old, new):
        db.add(
            TrackPoint(
                tracklet_id=track.id,
                frame_number=0,
                timestamp_seconds=0,
                bbox=[1, 2, 3, 4],
                shot_id="1",
                visibility_state="visible",
            )
        )
    metric = measurement(clip, old, 4)
    db.add(metric)
    await db.flush()
    for _ in range(2):
        await publish_job_tracks(db, job.id)
        await db.flush()
    labels = (
        (
            await db.execute(
                select(Label).where(Label.tracklet_id == new.id, Label.source == "inherited_human")
            )
        )
        .scalars()
        .all()
    )
    assert len(labels) == 1 and labels[0].label_value["inherited_from"] == str(label.id)
    assert new.provenance["exclusion_reason"] == "replacement_not_qualified"
    from app.routers.overlays import get_clip_overlays

    overlay = await get_clip_overlays(clip.id, db, user)
    assert {item.id for item in overlay.labels} == {label.id}


@pytest.mark.parametrize("status", [JobStatus.failed, JobStatus.succeeded])
async def test_fresh_detailed_clip_never_claims_final_before_promoted_outputs(
    db: AsyncSession, status: JobStatus
) -> None:
    from app.routers.clips import ClipCreate, create_clip

    user = await actor(db)
    video, _existing = await recording(db)
    job = ProcessingJob(
        id=uuid.uuid4(),
        video_id=video.id,
        job_type=JobType.pipeline,
        input_artifacts={"processing_profile": "detailed"},
    )
    db.add(job)
    await db.flush()
    proposed = await create_clip(
        video.id,
        ClipCreate(
            start_time=6,
            end_time=10,
            boundary_source="model",
            result_state="final",
            job_id=job.id,
            provenance={"source": "real"},
        ),
        db,
        user,
    )
    assert proposed.result_state == "preliminary"
    assert proposed.provenance["cuts_state"] == "ready"
    job.status = status
    if status == JobStatus.succeeded:
        await publish_job_tracks(db, job.id)
        await publish_job_events(db, job.id)
        await publish_job_metrics(db, job.id)
        await publish_job_clip_tiers(db, job)
    await db.flush()
    clip = await db.get(Clip, proposed.id)
    assert clip.result_state == "preliminary"


async def test_opportunities_include_unmeasured_reps_and_deduplicate_matched_views(
    db: AsyncSession,
) -> None:
    user = await actor(db)
    video, clip = await recording(db)
    alternate = Clip(id=uuid.uuid4(), video_id=video.id, start_time=10, end_time=15)
    other_rep = Clip(id=uuid.uuid4(), video_id=video.id, start_time=20, end_time=25)
    person = Player(
        id=uuid.uuid4(), first_name="Route", last_name=f"Opportunity{uuid.uuid4()}", position="WR"
    )
    session = FootballSession(id=uuid.uuid4(), title="Opportunity test")
    db.add_all([alternate, other_rep, person, session])
    await db.flush()
    video.practice_session_id = session.id
    play = Play(id=uuid.uuid4(), session_id=session.id)
    db.add(play)
    await db.flush()
    db.add_all(
        [
            PlayView(play_id=play.id, clip_id=clip.id),
            PlayView(play_id=play.id, clip_id=alternate.id),
        ]
    )
    tracks = [await player_track(db, c, person) for c in (clip, alternate, other_rep)]
    tracks[0].end_frame = 300
    for c, track in zip((clip, alternate, other_rep), tracks, strict=True):
        opportunity = measurement(c, track, 1)
        opportunity.metric_name = "position_opportunity"
        opportunity.unit = "rep"
        opportunity.metric_value = {
            "value": 1,
            "opportunity_kind": "routes",
            "opportunity_observation": True,
            "participation_event_id": str(uuid.uuid4()),
            "player_id": str(person.id),
            "canonical_play_id": "untrusted-packet-play",
        }
        db.add(opportunity)
    # One measured outcome among two eligible routes; alternate view must not add a rep.
    db.add(measurement(clip, tracks[0], 8))
    await db.flush()
    result = await query_analytics(
        AnalyticsQuery(session_ids=[session.id], metric_names=["distance_traveled"]), db, user
    )
    assert result["groups"][0]["n"] == 1
    assert result["opportunities"][0]["kind"] == "routes"
    assert result["opportunities"][0]["eligible_count"] == 2
    assert result["opportunities"][0]["player_count"] == 1
    assert {e["play_id"] for e in result["opportunities"][0]["evidence"]} == {
        str(play.id),
        str(other_rep.id),
    }
    assert result["cohort"]["player_reps"] == 2
    assert result["cohort"]["eligible_measurements"] == 1


async def test_unresolved_and_experimental_opportunities_never_become_denominators(
    db: AsyncSession,
) -> None:
    user = await actor(db)
    video, clip = await recording(db)
    session = FootballSession(id=uuid.uuid4(), title="Unknown opportunity test")
    person = Player(id=uuid.uuid4(), first_name="Unknown", last_name=f"Route{uuid.uuid4()}")
    db.add_all([session, person])
    await db.flush()
    video.practice_session_id = session.id
    unknown = await player_track(db, clip)
    resolved = await player_track(db, clip, person)
    for track, experimental in [(unknown, False), (resolved, True)]:
        opportunity = measurement(clip, track, 1)
        opportunity.metric_name = "position_opportunity"
        opportunity.unit = "rep"
        opportunity.metric_value = {
            "value": 1,
            "opportunity_kind": "routes",
            "opportunity_observation": True,
            "participation_event_id": str(uuid.uuid4()),
        }
        opportunity.experimental_flag = experimental
        db.add(opportunity)
    await db.flush()
    result = await query_analytics(AnalyticsQuery(session_ids=[session.id]), db, user)
    assert result["opportunities"] == []
    assert result["cohort"]["player_reps"] == 0
    assert {e["reason"] for e in result["exclusions"]} >= {"unresolved_player", "experimental"}


async def test_qualified_spatial_product_keeps_coordinates_without_fabricating_scalar(
    db: AsyncSession,
) -> None:
    user = await actor(db)
    video, clip = await recording(db)
    session = FootballSession(id=uuid.uuid4(), title="Spatial product test")
    person = Player(id=uuid.uuid4(), first_name="Punt", last_name=f"Landing{uuid.uuid4()}")
    db.add_all([session, person])
    await db.flush()
    video.practice_session_id = session.id
    track = await player_track(db, clip, person)
    landing = measurement(clip, track, 1)
    landing.metric_name = "kick_landing_location"
    landing.unit = "field_location"
    landing.metric_value = {
        "field_x": 33.5,
        "field_y": -12.5,
        "timestamp_seconds": 3.0,
        "visibility_state": "observed",
        "landing_event_id": "landing",
        "event_evidence": [
            {
                "event_id": "landing",
                "event_type": "landing",
                "timestamp_seconds": 3.0,
                "shot_id": "shot-a",
                "source": "human",
            }
        ],
    }
    db.add(landing)
    await db.flush()
    result = await query_analytics(AnalyticsQuery(session_ids=[session.id]), db, user)
    assert result["groups"] == []
    assert result["products"][0]["metric_name"] == "kick_landing_location"
    assert result["products"][0]["evidence"][0]["value"] is None
    assert result["products"][0]["evidence"][0]["metric_packet"]["field_y"] == -12.5
    assert result["cohort"]["eligible_measurements"] == 1


async def test_opportunity_only_qualification_changes_dataset_version(db: AsyncSession) -> None:
    from app.routers.analytics import summarize_rows

    video, clip = await recording(db)
    person = Player(id=uuid.uuid4(), first_name="Hash", last_name=f"Rep{uuid.uuid4()}")
    db.add(person)
    await db.flush()
    track = await player_track(db, clip, person)
    metric = measurement(clip, track, 1)
    metric.metric_name, metric.unit = "position_opportunity", "rep"
    metric.metric_value = {
        "value": 1,
        "opportunity_kind": "routes",
        "opportunity_observation": True,
        "participation_event_id": str(uuid.uuid4()),
    }
    metric.experimental_flag = True
    db.add(metric)
    await db.flush()
    rows = [(metric, clip, video, track, person, clip.id)]
    before = summarize_rows(rows, AnalyticsQuery())
    metric.experimental_flag = False
    after = summarize_rows(rows, AnalyticsQuery())
    assert before["opportunities"] == [] and after["opportunities"][0]["eligible_count"] == 1
    assert before["dataset_version"] != after["dataset_version"]


async def test_partial_movement_stays_film_evidence_without_complete_scalar(
    db: AsyncSession,
) -> None:
    from app.routers.analytics import summarize_rows

    video, clip = await recording(db)
    person = Player(id=uuid.uuid4(), first_name="Partial", last_name=f"Path{uuid.uuid4()}")
    db.add(person)
    await db.flush()
    track = await player_track(db, clip, person)
    metric = measurement(clip, track, 9)
    metric.metric_name = "observed_separation"
    metric.metric_value = {
        "yards": 9,
        "partial_observation": True,
        "measurement_complete": False,
        "series": [
            {"timestamp_seconds": 0.1, "yards": 9, "shot_id": "a", "visibility_state": "observed"}
        ],
    }
    db.add(metric)
    await db.flush()
    result = summarize_rows([(metric, clip, video, track, person, clip.id)], AnalyticsQuery())
    assert result["groups"] == []
    assert result["censoring"] == []
    assert result["products"][0]["evidence"][0]["value"] is None
    assert result["products"][0]["evidence"][0]["metric_packet"]["partial_observation"] is True


@pytest.mark.parametrize(
    ("listed", "declared", "cohort", "included"),
    [
        ("C", None, "OL", True),
        ("LT", None, "OL", True),
        ("CB", None, "DB", True),
        ("DT", None, "DL", True),
        ("MLB", None, "LB", True),
        ("K", None, "ST", True),
        ("ATH", "Skill", "WR", False),
        ("DT", "Skill", "DL", False),
        ("WR", "Skill", "WR", True),
        ("CB", "DL", "DB", False),
        ("CB", "DL", "DL", True),
        ("DE", None, "EDGE", False),
        ("EDGE", None, "EDGE", True),
    ],
)
async def test_position_tabs_select_declared_roster_cohorts_without_role_inference(
    db: AsyncSession, listed, declared, cohort, included
) -> None:
    user = await actor(db)
    video, clip = await recording(db)
    session = FootballSession(id=uuid.uuid4(), title="Position alias cohort")
    player = Player(
        id=uuid.uuid4(),
        first_name="Cohort",
        last_name=f"Alias{uuid.uuid4()}",
        position=listed,
        position_group=declared,
    )
    db.add_all([session, player])
    await db.flush()
    video.practice_session_id = session.id
    track = await player_track(db, clip, player)
    db.add(measurement(clip, track, 4))
    await db.flush()
    result = await query_analytics(
        AnalyticsQuery(position=cohort, session_ids=[session.id]), db, user
    )
    assert bool(result["groups"]) is included


async def test_expected_prediction_ingest_reaches_only_actual_player_cohort(
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Protocol-only approved metadata tests persistence/filtering, never accuracy."""
    from app.models import ModelStage, ModelVersion, WorkerRuntime
    from app.routers.jobs import JobStatusUpdate, update_job_status
    from app.routers.metrics import MetricCreate, create_metric
    from app.services.expected_outcomes import FEATURE_VERSION, POPULATIONS
    from app.services.metric_definitions import DEFINITIONS
    from app.services.metric_publication import trusted_metric_job
    from fastapi import BackgroundTasks
    from starlette.requests import Request

    user = await actor(db)
    monkeypatch.setenv("SEED_WORKER_EMAIL", user.email)
    get_settings.cache_clear()
    video, clip = await recording(db)
    worker_id = f"prediction-{uuid.uuid4()}"
    job = ProcessingJob(
        id=uuid.uuid4(),
        video_id=video.id,
        job_type=JobType.pipeline,
        status=JobStatus.running,
        leased_by=worker_id,
        lease_token=uuid.uuid4(),
        attempt_count=1,
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    db.add_all([job, WorkerRuntime(worker_id=worker_id, registered_by=user.id)])
    await db.flush()
    producer = await trusted_metric_job(
        Request(
            {
                "type": "http",
                "headers": [
                    (b"x-job-id", str(job.id).encode()),
                    (b"x-worker-id", worker_id.encode()),
                    (b"x-job-lease-token", str(job.lease_token).encode()),
                ],
            }
        ),
        db,
        user,
    )
    session = FootballSession(id=uuid.uuid4(), title="Prediction protocol")
    players = [
        Player(id=uuid.uuid4(), first_name="Fixture", last_name=f"QB-{uuid.uuid4()}", position="QB")
        for _ in range(2)
    ]
    definition = DEFINITIONS["expected_completion"]
    model = ModelVersion(
        id=uuid.uuid4(),
        model_name="fixture-protocol-only",
        version="fixture",
        model_type="expected_completion",
        promoted_stage=ModelStage.production,
        metrics={
            "approved_evaluation_id": "protocol-only",
            "approved_artifact_sha256": "b" * 64,
            "publication_approvals": {
                "expected_completion": {
                    "feature_version": FEATURE_VERSION,
                    "definition_version": definition["version"],
                    "capture_regimes": ["fixed_sideline"],
                }
            },
        },
    )
    db.add_all([session, model, *players])
    await db.flush()
    video.practice_session_id, video.content_sha256, video.size_bytes = session.id, "a" * 64, 1024
    clip.capture_regime = "fixed_sideline"
    for player, probability in zip(players, (0.6, 0.7), strict=True):
        track = await player_track(db, clip, player)
        packet = {
            "task": "completion",
            "feature_version": FEATURE_VERSION,
            "weights_sha256": "b" * 64,
            "eligible_population": POPULATIONS["completion"],
            "value": probability,
            "validated": True,
            "experimental": False,
            "analytics_safe": True,
            "capture_regime": "fixed_sideline",
            "definition_version": definition["version"],
            "feature_evidence_sha256": ["c" * 64],
            "observation_clock": {"event": "pass_release", "timestamp_seconds": 0.5},
            "lineage": {
                "recording_id": str(video.id),
                "session_id": str(session.id),
                "canonical_play_id": str(clip.id),
                "media_sha256": "a" * 64,
            },
            "subject": {"scope": "player", "role": "QB", "tracklet_id": str(track.id)},
        }
        provenance = {
            "source": "real",
            "validated": True,
            "artifact_sha256": "b" * 64,
            "approved_evaluation_id": "protocol-only",
            "quality_checks_passed": True,
            "definition_version": definition["version"],
            "evidence": {key: True for key in definition["required_evidence"]},
        }
        created = await create_metric(
            MetricCreate(
                clip_id=clip.id,
                tracklet_id=track.id,
                metric_name="expected_completion",
                metric_value=packet,
                unit="prob",
                experimental_flag=False,
                analytics_safe=True,
                model_version_id=model.id,
                job_id=job.id,
                provenance=provenance,
            ),
            db,
            user,
            producer,
        )
        assert created.analytics_safe and created.tracklet_id == track.id
        assert created.provenance["publication_state"] == "pending"
    await update_job_status(
        job.id,
        JobStatusUpdate(
            status=JobStatus.succeeded,
            worker_id=worker_id,
            lease_token=job.lease_token,
            attempt_count=job.attempt_count,
        ),
        db,
        user,
        BackgroundTasks(),
    )
    result = await query_analytics(
        AnalyticsQuery(
            metric_names=["expected_completion"], player_id=players[0].id, session_ids=[session.id]
        ),
        db,
        user,
    )
    assert result["cohort"]["eligible_measurements"] == 1
    assert result["groups"][0]["mean"] == 0.6
    assert result["groups"][0]["evidence"][0]["player_id"] == str(players[0].id)


async def test_dense_manual_event_pages_preserve_complete_effective_set(db: AsyncSession) -> None:
    from app.routers.events import list_events_for_clip

    user = await actor(db)
    _, clip = await recording(db)
    manual = [
        Event(
            id=uuid.uuid4(),
            clip_id=clip.id,
            event_type="protection_envelope",
            timestamp_seconds=i / 1000,
            attributes={"sample": i},
            provenance={"source": "manual"},
            created_by=user.id,
        )
        for i in range(1201)
    ]
    db.add_all(manual)
    # A machine event with the same type on another page is overridden by staff.
    db.add(
        Event(
            id=uuid.uuid4(),
            clip_id=clip.id,
            event_type="protection_envelope",
            timestamp_seconds=2,
            attributes={"source": "real"},
            provenance={"source": "real"},
        )
    )
    await db.flush()
    pages = [
        await list_events_for_clip(clip.id, db, user, None, 500, offset, True)
        for offset in (0, 500, 1000)
    ]
    actual = [row for page in pages for row in page]
    assert [row.id for row in actual] == [row.id for row in manual]
    assert actual[-1].attributes["sample"] == 1200
    general = await list_events_for_clip(clip.id, db, user, None, 500, 1000, False)
    assert [row.id for row in general] == [row.id for row in manual[1000:]]
