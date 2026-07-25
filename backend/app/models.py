"""ORM models for Football-IQ.

Tables:
    users              — platform user accounts and roles
    players            — Toledo athlete roster
    videos             — raw uploaded video records
    clips              — per-play clip segments
    clip_players       — many-to-many: clips ↔ players
    processing_jobs    — async job tracking for the CV pipeline
    model_versions     — ML model lineage and promotion tracking
    field_calibrations — homography matrix + confidence for each video
    tracklets          — player track segments within a clip
    track_points       — individual (frame, x, y) observations in a tracklet
    events             — tagged game events within a clip
    labels             — ground-truth labels for model training
    coach_corrections  — human overrides of model outputs
    metrics            — computed analytics metrics linked to clips
    training_datasets  — versioned snapshots exported for model training
    active_learning_queue — prioritized samples for human relabeling
"""

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
    true,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.types import Vector

# ── Enumerations ──────────────────────────────────────────────────────────────


class UserRole(enum.StrEnum):
    admin = "admin"
    analyst = "analyst"
    coach = "coach"
    sportsperformance = "sportsperformance"
    player = "player"
    viewer = "viewer"


class JobStatus(enum.StrEnum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"


class JobType(enum.StrEnum):
    #: Full chain: ingest → … → render, orchestrated in-process by the GPU
    #: worker (pipeline.orchestrator). The normal coach-facing job type.
    pipeline = "pipeline"
    ingest = "ingest"
    segment = "segment"
    calibrate = "calibrate"
    detect = "detect"
    track = "track"
    reid = "reid"
    pose = "pose"
    events = "events"
    labels = "labels"
    metrics = "metrics"
    pressure = "pressure"
    render = "render"
    render_hls = "render_hls"
    routes = "routes"
    coverage = "coverage"
    oline = "oline"
    self_scout = "self_scout"
    embeddings = "embeddings"
    workload_rollup = "workload_rollup"
    #: Scheduled model retraining from exported coach corrections (P3).
    train = "train"


class PipelineMode(enum.StrEnum):
    same_session = "same_session"
    nightly = "nightly"


class ClipResultState(enum.StrEnum):
    """Quality tier of a clip's analysis results (Issue #147).

    ``preliminary`` — produced by the same-session lightweight path during the
        period-break window. Detections / tracks / labels exist but the clip has
        not yet been through nightly full-quality post-processing, so the coach
        sees a "Preliminary" badge.
    ``final`` — produced or upgraded by the nightly full-quality path. The
        nightly run replaces the preliminary results in place (see the
        ``/clips/finalize`` endpoint), flipping the clip to ``final``.

    Stored as a plain ``String`` (mirrors ``ProcessingJob.pipeline_mode``) rather
    than a DB enum so the column is additive and migrations stay simple. A NULL
    value means "legacy / unknown" — such clips predate the same-session split
    and are not treated as preliminary.
    """

    preliminary = "preliminary"
    final = "final"


class ModelStage(enum.StrEnum):
    experimental = "experimental"
    staging = "staging"
    production = "production"
    retired = "retired"


class VideoStatus(enum.StrEnum):
    uploaded = "uploaded"
    processing = "processing"
    ready = "ready"
    failed = "failed"


class SessionKind(enum.StrEnum):
    """Operating mode of the capture session. See ADR 0001."""

    practice = "practice"
    scrimmage = "scrimmage"
    game = "game"


class SourceType(enum.StrEnum):
    """How the video reached Football-IQ. ``drone`` is the default capture path
    (single overhead camera per the ADR); ``uploaded_clip`` covers manual
    coach uploads of pre-segmented clips."""

    drone = "drone"
    uploaded_clip = "uploaded_clip"


class SideOfBall(enum.StrEnum):
    """Shared vocabulary for ``Video.our_possession``, ``Clip.our_possession``,
    and ``Clip.side_of_ball``. ``Tracklet.side_of_ball`` accepts the same
    values but remains a ``String`` for now (see ADR 0001 §4)."""

    offense = "offense"
    defense = "defense"
    special_teams = "special_teams"


class CorrectionType(enum.StrEnum):
    clip_boundary = "clip_boundary"
    player_identity = "player_identity"
    event_tag = "event_tag"
    formation_tag = "formation_tag"
    route_tag = "route_tag"
    coverage_tag = "coverage_tag"
    personnel_tag = "personnel_tag"
    leverage_tag = "leverage_tag"
    effort_tag = "effort_tag"
    pose_biomechanics_tag = "pose_biomechanics_tag"


class ActiveLearningReason(enum.StrEnum):
    low_confidence = "low_confidence"
    uncertainty_sampling = "uncertainty_sampling"
    regression = "regression"
    hard_negative = "hard_negative"


class ActiveLearningStatus(enum.StrEnum):
    queued = "queued"
    in_review = "in_review"
    resolved = "resolved"


class AlertType(enum.StrEnum):
    bio_deviation = "bio_deviation"
    effort_anomaly = "effort_anomaly"
    formation_anomaly = "formation_anomaly"
    # Self-scout tendency-break alerts (Issue #137): a formation/personnel/
    # situation tuple whose run-or-pass tendency is so lopsided that it is
    # coaching-actionable (season tendency), or an in-game pattern break where
    # the current game diverges sharply from the season baseline.
    formation_tendency = "formation_tendency"
    # Workload-risk alerts (Issue #149): ACWR > 1.5 or gait asymmetry > 1.3.
    # Sports-performance staff only — never surfaced to coaches, players, or
    # viewers (see RESTRICTED_ALERT_TYPES in the alerts router).
    workload_risk = "workload_risk"


class AlertSeverity(enum.StrEnum):
    low = "low"
    medium = "medium"
    high = "high"


class CaptureRegime(enum.StrEnum):
    """Source-capture regime inferred from pixels at ingest (Issue #126).

    Toledo film arrives without SRT/GPS/IMU, so every Phase-CV stage routes on
    whichever of these the ingest-time detector picked. ``unconstrained`` is
    the first-class "any camera / any angle / any height" regime — analyzed
    footage that matches neither special regime takes the generic pipeline
    path (ADR 0005). ``unknown`` is reserved for hard analysis failures and
    is the backfill value for rows that predate this column.
    """

    drone_follow = "drone_follow"
    fixed_sideline = "fixed_sideline"
    unconstrained = "unconstrained"
    unknown = "unknown"


class PlayerVisibilityState(enum.StrEnum):
    """Outward-facing visibility lifecycle for a player profile (Issue #114).

    Default is ``staff_only`` — content stays internal until a coach or analyst
    explicitly approves it for player-facing or recruiting consumption.  See
    ``app.governance.VisibilityState`` for the runtime enum used by the
    governance helpers; this duplicates the values at the schema layer so the
    database enum is independently versioned by migrations.
    """

    staff_only = "staff_only"
    player_approved = "player_approved"
    recruiting_approved = "recruiting_approved"
    archived = "archived"


class ReportFormat(enum.StrEnum):
    """Output format for a generated coaching report (Issue #111)."""

    pdf = "pdf"
    csv = "csv"
    json = "json"


class ProfileGenerationSource(enum.StrEnum):
    """How a player profile / development snapshot was produced (Issue #7).

    Recorded as ``generated_by`` so a coach always knows whether a snapshot was
    hand-authored, derived from model outputs, or imported from an external
    record before it is approved for player-facing use.
    """

    manual = "manual"
    model_assisted = "model_assisted"
    imported = "imported"


class PlayerIdentityState(enum.StrEnum):
    """Confidence-scored identity state for a development snapshot (Issue #7).

    Jersey OCR is never treated as ground truth — practice clips include
    temporary/non-assigned jerseys, pinnies, wrong numbers, or unreadable
    numbers, so the resolved identity carries an explicit state alongside its
    confidence. Low-confidence states (everything other than ``known`` /
    ``probable``) can never be silently attached to a player-facing profile.
    """

    known = "known"
    probable = "probable"
    unknown = "unknown"
    conflicting = "conflicting"
    needs_review = "needs_review"


class SnapshotApprovalStatus(enum.StrEnum):
    """Approval lifecycle for a weekly development snapshot (Issue #7).

    A snapshot starts ``pending`` and must be explicitly ``approved`` by a
    coach/analyst before it participates in any player-facing or recruiting
    projection. ``rejected`` keeps the row for audit without surfacing it.
    """

    pending = "pending"
    approved = "approved"
    rejected = "rejected"


# ── Models ────────────────────────────────────────────────────────────────────


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, name="user_role"), nullable=False, default=UserRole.viewer
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class Player(Base):
    __tablename__ = "players"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    first_name: Mapped[str] = mapped_column(String(100), nullable=False)
    last_name: Mapped[str] = mapped_column(String(100), nullable=False)
    jersey_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    position: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # Coaching position group (QB, Skill, OL, DL, LB, DB, ST). Indexed because
    # position-coach views filter on this rather than the raw position string.
    position_group: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    # Link to platform account (optional — players may not have login)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    metadata_: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSON, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Outward-facing visibility state (Issue #114).  Defaults to staff_only —
    # nothing leaks to player or recruiting projections until explicitly
    # approved by an authorized staff member.
    visibility_state: Mapped[PlayerVisibilityState] = mapped_column(
        Enum(PlayerVisibilityState, name="player_visibility_state"),
        nullable=False,
        default=PlayerVisibilityState.staff_only,
        server_default=PlayerVisibilityState.staff_only.value,
    )
    visibility_updated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    visibility_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    user: Mapped["User | None"] = relationship("User", foreign_keys=[user_id])
    clips: Mapped[list["Clip"]] = relationship(
        "Clip", secondary="clip_players", back_populates="players"
    )


class Video(Base):
    __tablename__ = "videos"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    storage_uri: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[VideoStatus] = mapped_column(
        Enum(VideoStatus, name="video_status"), nullable=False, default=VideoStatus.uploaded
    )
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    fps: Mapped[float | None] = mapped_column(Float, nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    codec: Mapped[str | None] = mapped_column(String(50), nullable=True)
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    recorded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    # ── Session metadata (ADR 0001 / #97) ─────────────────────────────────
    session_kind: Mapped[SessionKind | None] = mapped_column(
        Enum(SessionKind, name="session_kind"), nullable=True, index=True
    )
    source_type: Mapped[SourceType | None] = mapped_column(
        Enum(SourceType, name="source_type"), nullable=True
    )
    opponent_team: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    practice_session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    our_possession: Mapped[SideOfBall | None] = mapped_column(
        Enum(SideOfBall, name="side_of_ball"), nullable=True
    )
    # ── Capture regime (Issue #126) ───────────────────────────────────────
    # Inferred from pixels at ingest. Denormalized onto clip rows when
    # clips are created so downstream stages don't need to JOIN videos.
    capture_regime: Mapped[CaptureRegime | None] = mapped_column(
        Enum(CaptureRegime, name="capture_regime", create_type=False),
        nullable=True,
        index=True,
    )
    regime_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    metadata_: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    uploader: Mapped["User | None"] = relationship("User", foreign_keys=[uploaded_by])
    clips: Mapped[list["Clip"]] = relationship("Clip", back_populates="video")
    jobs: Mapped[list["ProcessingJob"]] = relationship("ProcessingJob", back_populates="video")
    calibrations: Mapped[list["FieldCalibration"]] = relationship(
        "FieldCalibration", back_populates="video"
    )


class Clip(Base):
    __tablename__ = "clips"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    video_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("videos.id", ondelete="CASCADE"), nullable=False
    )
    storage_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    start_time: Mapped[float] = mapped_column(Float, nullable=False)  # seconds
    end_time: Mapped[float] = mapped_column(Float, nullable=False)  # seconds
    play_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # ── Cross-regime pairing (Issue #150) ─────────────────────────────────────
    # Coach-tagged play-call code shared verbatim between the practice
    # (``drone_follow``) clip and the game (``fixed_sideline``) clip of the same
    # play. Two clips form a "paired play" when they carry the same non-null
    # ``play_call_id``; the cross-regime self-distillation trainer aligns the
    # game pseudo-labels onto the practice clip via this key. Indexed because the
    # aligner groups clips by it.
    play_call_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    label_data: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_reviewed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # Boundary source/confidence — set by Stage 2 (segmentation)
    # "model" = auto-proposed, "manual" = coach override
    boundary_source: Mapped[str | None] = mapped_column(String(20), nullable=True)
    boundary_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Version lineage
    model_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("model_versions.id", ondelete="SET NULL"), nullable=True
    )
    calibration_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("field_calibrations.id", ondelete="SET NULL"),
        nullable=True,
    )
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("processing_jobs.id", ondelete="SET NULL"), nullable=True
    )
    # ── Phase 2 columns ───────────────────────────────────────────────────
    personnel_grouping: Mapped[str | None] = mapped_column(String(20), nullable=True)
    down: Mapped[int | None] = mapped_column(Integer, nullable=True)
    distance: Mapped[float | None] = mapped_column(Float, nullable=True)
    field_zone: Mapped[str | None] = mapped_column(String(30), nullable=True)

    # ── Session / possession metadata (ADR 0001 / #98) ────────────────────
    # ``session_kind`` is denormalized from the parent video at write time so
    # clip-level queries don't have to JOIN videos.
    session_kind: Mapped[SessionKind | None] = mapped_column(
        Enum(SessionKind, name="session_kind", create_type=False), nullable=True, index=True
    )
    # ``our_possession`` is the Toledo side on this play (required for game
    # clips, inherited from the parent session otherwise).
    our_possession: Mapped[SideOfBall | None] = mapped_column(
        Enum(SideOfBall, name="side_of_ball", create_type=False), nullable=True
    )
    # ``side_of_ball`` is the per-clip side promoted from ``label_data``.
    # Same vocabulary as ``our_possession``; see ADR 0001 §3.
    side_of_ball: Mapped[SideOfBall | None] = mapped_column(
        Enum(SideOfBall, name="side_of_ball", create_type=False), nullable=True, index=True
    )

    # ── Capture regime (Issue #126) ───────────────────────────────────────
    # Denormalized from the parent video at clip-create time. ``unknown``
    # is the safe fallback when the detector can't classify confidently.
    capture_regime: Mapped[CaptureRegime | None] = mapped_column(
        Enum(CaptureRegime, name="capture_regime", create_type=False),
        nullable=True,
        index=True,
    )
    regime_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

    # ── Active-learning uncertainty (Issues #145 / #146) ──────────────────────
    # Clip-level calibrated uncertainty (normalized entropy, in [0, 1]) of the
    # Phase-2 ensemble for this play; higher = the model is less sure, so it is
    # prioritized in the coach-correction annotation queue. NULL means "not yet
    # scored". ``uncertainty_calibrated`` records whether the contributing model
    # outputs were calibrated (Issue #146) — an uncalibrated score must never be
    # presented to a coach as a trusted confidence. Indexed because the
    # annotation queue orders by it.
    uncertainty_score: Mapped[float | None] = mapped_column(Float, nullable=True, index=True)
    uncertainty_calibrated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )

    # ── Same-session result tier (Issue #147) ─────────────────────────────────
    # Whether this clip's analysis is a same-session ``preliminary`` first pass
    # or a nightly-upgraded ``final`` result. NULL = legacy/unknown (predates the
    # same-session split). Indexed so the Practice Inbox can count/filter clips
    # still awaiting their nightly upgrade. See ``ClipResultState``.
    result_state: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    video: Mapped["Video"] = relationship("Video", back_populates="clips")
    reviewer: Mapped["User | None"] = relationship("User", foreign_keys=[reviewed_by])
    players: Mapped[list["Player"]] = relationship(
        "Player", secondary="clip_players", back_populates="clips"
    )
    jobs: Mapped[list["ProcessingJob"]] = relationship(
        "ProcessingJob", back_populates="clip", foreign_keys="ProcessingJob.clip_id"
    )
    tracklets: Mapped[list["Tracklet"]] = relationship("Tracklet", back_populates="clip")
    events: Mapped[list["Event"]] = relationship("Event", back_populates="clip")
    metrics: Mapped[list["Metric"]] = relationship("Metric", back_populates="clip")
    corrections: Mapped[list["CoachCorrection"]] = relationship(
        "CoachCorrection", back_populates="clip"
    )


class ClipPlayer(Base):
    """Association table linking clips to the players appearing in them."""

    __tablename__ = "clip_players"

    clip_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("clips.id", ondelete="CASCADE"),
        primary_key=True,
    )
    player_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("players.id", ondelete="CASCADE"),
        primary_key=True,
    )


class ProcessingJob(Base):
    __tablename__ = "processing_jobs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    video_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("videos.id", ondelete="SET NULL"), nullable=True
    )
    clip_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clips.id", ondelete="SET NULL"), nullable=True
    )
    job_type: Mapped[JobType] = mapped_column(Enum(JobType, name="job_type"), nullable=False)
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, name="job_status"), nullable=False, default=JobStatus.queued
    )
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pipeline_mode: Mapped[str | None] = mapped_column(
        String(20), nullable=True, default=None, index=True
    )
    # ── DB-as-queue lease fields ──────────────────────────────────────────
    # The GPU worker claims queued rows via POST /jobs/claim (FOR UPDATE
    # SKIP LOCKED) and keeps its lease alive via POST /jobs/{id}/heartbeat.
    # An expired lease makes the row claimable again until attempt_count
    # reaches max_attempts, at which point the claim sweep fails it.
    leased_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    #: Per-stage progress map maintained by the orchestrator via heartbeat:
    #: ``{stage[:clip] : {"status": ..., "at": ..., ...headline numbers}}``.
    progress: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_stage: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    nightly_followup_job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    input_artifacts: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    output_artifacts: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    model_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("model_versions.id", ondelete="SET NULL"),
        nullable=True,
    )
    training_dataset_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("training_datasets.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    video: Mapped["Video | None"] = relationship("Video", back_populates="jobs")
    clip: Mapped["Clip | None"] = relationship(
        "Clip", back_populates="jobs", foreign_keys=[clip_id]
    )
    model_version: Mapped["ModelVersion | None"] = relationship(
        "ModelVersion", back_populates="jobs"
    )


class ModelVersion(Base):
    __tablename__ = "model_versions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    model_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    version: Mapped[str] = mapped_column(String(100), nullable=False)
    model_type: Mapped[str] = mapped_column(String(100), nullable=False)
    artifact_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    training_dataset_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("training_datasets.id", ondelete="SET NULL"),
        nullable=True,
    )
    metrics: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    promoted_stage: Mapped[ModelStage] = mapped_column(
        Enum(ModelStage, name="model_stage"),
        nullable=False,
        default=ModelStage.experimental,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    jobs: Mapped[list["ProcessingJob"]] = relationship(
        "ProcessingJob", back_populates="model_version"
    )


# ── Phase 1 tables ────────────────────────────────────────────────────────────


class FieldCalibration(Base):
    """Homography matrix mapping pixel coordinates to standardized field coords."""

    __tablename__ = "field_calibrations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    video_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("videos.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Homography matrix stored as a flat 9-element JSON array (row-major)
    homography: Mapped[list[float] | None] = mapped_column(JSON, nullable=True)
    # 0.0–1.0 confidence from calibration pipeline; metrics are suppressed below threshold
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    confidence_threshold: Mapped[float] = mapped_column(Float, nullable=False, default=0.7)
    # True when confidence >= threshold AND no disqualifying reason codes
    analytics_safe: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Calibration failure/warning reason codes (e.g. ["low_contrast", "partial_field"])
    reason_codes: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    # Key pixel-to-field point pairs used for calibration
    calibration_points: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # ── Regime-aware calibration diagnostics (Issue #127 / #138) ──────────────
    # 9-vector Kalman state vec(H) for DRONE_FOLLOW nightly smoothing
    kalman_state: Mapped[list[float] | None] = mapped_column(JSON, nullable=True)
    # RANSAC inlier ratio of the chosen homography fit
    inlier_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Count of field lines detected on the calibration frame
    line_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Angular variance of detected yard lines (lower = cleaner parallelism)
    parallel_variance: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Mean inter-window re-projection drift (px); 0.0 for a fixed anchor
    temporal_drift: Mapped[float | None] = mapped_column(Float, nullable=True)
    # True for the once-per-game FIXED_SIDELINE anchor reused across plays
    is_game_anchor: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    model_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("model_versions.id", ondelete="SET NULL"),
        nullable=True,
    )
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("processing_jobs.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    video: Mapped["Video"] = relationship("Video", back_populates="calibrations")


class Tracklet(Base):
    """A continuous track for a single player within a clip."""

    __tablename__ = "tracklets"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    clip_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clips.id", ondelete="CASCADE"), nullable=False, index=True
    )
    player_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("players.id", ondelete="SET NULL"), nullable=True
    )
    # Frame range within the clip
    start_frame: Mapped[int] = mapped_column(Integer, nullable=False)
    end_frame: Mapped[int] = mapped_column(Integer, nullable=False)
    # 0.0–1.0 track confidence from the tracking model
    track_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Team label ("home" / "away" / "unknown") for team-level tracking before identity
    team_label: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # Phase 2: position group and side of ball for downstream analytics
    position_group: Mapped[str | None] = mapped_column(String(20), nullable=True)
    side_of_ball: Mapped[str | None] = mapped_column(String(10), nullable=True)
    model_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("model_versions.id", ondelete="SET NULL"),
        nullable=True,
    )
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("processing_jobs.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    clip: Mapped["Clip"] = relationship("Clip", back_populates="tracklets")
    player: Mapped["Player | None"] = relationship("Player")
    track_points: Mapped[list["TrackPoint"]] = relationship(
        "TrackPoint", back_populates="tracklet", order_by="TrackPoint.frame_number"
    )


class TrackPoint(Base):
    """A single (frame, x, y, bbox) observation within a tracklet."""

    __tablename__ = "track_points"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tracklet_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tracklets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    frame_number: Mapped[int] = mapped_column(Integer, nullable=False)
    # Field coordinates (yards) — null when calibration confidence is below threshold
    field_x: Mapped[float | None] = mapped_column(Float, nullable=True)
    field_y: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Pixel bounding box [x1, y1, x2, y2]
    bbox: Mapped[list[float] | None] = mapped_column(JSON, nullable=True)
    detection_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

    tracklet: Mapped["Tracklet"] = relationship("Tracklet", back_populates="track_points")


class Event(Base):
    """A tagged game event (snap, motion, penalty, etc.) within a clip."""

    __tablename__ = "events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    clip_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clips.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    frame_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    timestamp_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    attributes: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    model_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("model_versions.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    clip: Mapped["Clip"] = relationship("Clip", back_populates="events")
    creator: Mapped["User | None"] = relationship("User", foreign_keys=[created_by])


class Label(Base):
    """Ground-truth label for model training, linked to a clip or tracklet."""

    __tablename__ = "labels"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    clip_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clips.id", ondelete="SET NULL"), nullable=True, index=True
    )
    tracklet_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tracklets.id", ondelete="SET NULL"), nullable=True
    )
    label_type: Mapped[str] = mapped_column(String(100), nullable=False)
    label_value: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    annotated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # Source: "model" (auto-label) or "human" (coach/analyst correction)
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="human")
    dataset_version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    model_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("model_versions.id", ondelete="SET NULL"),
        nullable=True,
    )
    training_dataset_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("training_datasets.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    annotator: Mapped["User | None"] = relationship("User", foreign_keys=[annotated_by])


class CoachCorrection(Base):
    """A human override of a model output — becomes a training label."""

    __tablename__ = "coach_corrections"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    clip_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clips.id", ondelete="CASCADE"), nullable=False, index=True
    )
    correction_type: Mapped[CorrectionType] = mapped_column(
        Enum(CorrectionType, name="correction_type"), nullable=False
    )
    # Original model output before correction
    original_value: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # Corrected value provided by the coach/analyst
    corrected_value: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    corrected_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    training_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Whether this correction has been exported as a training label
    exported_as_label: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    clip: Mapped["Clip"] = relationship("Clip", back_populates="corrections")
    corrector: Mapped["User"] = relationship("User", foreign_keys=[corrected_by])


class Metric(Base):
    """A computed analytics metric for a clip, always linked to its evidence."""

    __tablename__ = "metrics"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    clip_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clips.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Optional link to the specific player tracklet this metric belongs to
    tracklet_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tracklets.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    metric_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    metric_value: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    unit: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # Suppressed when field calibration confidence is below threshold
    is_suppressed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    suppression_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Head orientation and other experimental metrics: must be reviewed by a position
    # coach before being surfaced in player-facing views.
    experimental_flag: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # True only after a position coach explicitly approves this metric for staff views
    analytics_safe: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # 0.0–1.0 model confidence for this specific metric value
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Effort-review scalar fields from issue #142. These duplicate the JSON
    # payload for indexed/queryable access while the full review context stays
    # in metric_value.
    effort_zscore: Mapped[float | None] = mapped_column(Float, nullable=True)
    loaf_flag: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # Workload-fusion scalar fields from Issue #149. Like the effort fields
    # above these duplicate metric_value keys for indexed/queryable access;
    # they are experimental sports-performance signals, never a diagnosis.
    sprint_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    asymmetry_index: Mapped[float | None] = mapped_column(Float, nullable=True)
    injury_risk_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    # URI to the evidence artifact (e.g. annotated frame, track overlay)
    evidence_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Full version lineage for every metric
    model_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("model_versions.id", ondelete="SET NULL"),
        nullable=True,
    )
    calibration_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("field_calibrations.id", ondelete="SET NULL"),
        nullable=True,
    )
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("processing_jobs.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    clip: Mapped["Clip"] = relationship("Clip", back_populates="metrics")
    tracklet: Mapped["Tracklet | None"] = relationship("Tracklet")
    reviews: Mapped[list["HeadOrientationReview"]] = relationship(
        "HeadOrientationReview", back_populates="metric"
    )


# ── Head orientation tables ────────────────────────────────────────────────────


class PoseKeypoints(Base):
    """Per-frame pose keypoints extracted from a player tracklet.

    Used to derive head-direction vectors (yaw angle) for QB progression reads,
    safety eye discipline, CB technique, and LB play-action response analysis.
    This is head-orientation estimation — not true eye tracking.
    """

    __tablename__ = "pose_keypoints"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tracklet_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tracklets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    frame_number: Mapped[int] = mapped_column(Integer, nullable=False)
    # Full keypoint array: list of {name, x, y, confidence} dicts from RTMPose/ViTPose
    keypoints: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    # Derived head-direction yaw angle in degrees (0° = facing field direction)
    head_yaw_degrees: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 0.0–1.0 confidence based on keypoint visibility and head occlusion
    head_orientation_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Per-frame computed biomechanics angles (hip_flexion_degrees, torso_angle_degrees, etc.)
    biomechanics: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    model_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("model_versions.id", ondelete="SET NULL"),
        nullable=True,
    )
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("processing_jobs.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    tracklet: Mapped["Tracklet"] = relationship("Tracklet")


class HeadOrientationReview(Base):
    """Position-coach review of an experimental head-orientation metric.

    A metric with experimental_flag=True must be approved by the relevant
    position coach before analytics_safe is set and staff views are unlocked.
    Player-facing views never show experimental metrics regardless of this status.
    """

    __tablename__ = "head_orientation_reviews"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    metric_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("metrics.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    reviewer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    # "approve" → sets metric.analytics_safe=True
    # "reject"  → metric stays suppressed; model team is notified
    # "flag"    → routed to model review queue
    review_action: Mapped[str] = mapped_column(String(20), nullable=False)
    # Position group this review applies to: "QB", "Safety", "CB", "LB"
    position_group: Mapped[str | None] = mapped_column(String(50), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    metric: Mapped["Metric"] = relationship("Metric", back_populates="reviews")
    reviewer: Mapped["User"] = relationship("User", foreign_keys=[reviewer_id])


# ── MLOps tables ───────────────────────────────────────────────────────────────


class TrainingDataset(Base):
    """Versioned snapshot of labels exported for a specific model scope."""

    __tablename__ = "training_datasets"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    snapshot_date: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    model_scope: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    source_label_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    source_correction_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    artifact_uri: Mapped[str] = mapped_column(Text, nullable=False)
    changelog: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Alert(Base):
    """Real-time coaching alert generated by anomaly detection.

    Governance:
      - Never shown to players.
      - Bio deviation alerts require pose pipeline active + coach-approved
        for the position group.
      - Every alert carries: confidence, clip_uri, player_id, timestamp,
        position_group.
    """

    __tablename__ = "alerts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    player_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("players.id", ondelete="SET NULL"), nullable=True, index=True
    )
    clip_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clips.id", ondelete="SET NULL"), nullable=True, index=True
    )
    position_group: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    alert_type: Mapped[AlertType] = mapped_column(
        Enum(AlertType, name="alert_type"), nullable=False, index=True
    )
    severity: Mapped[AlertSeverity] = mapped_column(
        Enum(AlertSeverity, name="alert_severity"), nullable=False
    )
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    metric_name: Mapped[str] = mapped_column(String(255), nullable=False)
    metric_value: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    deviation_sd: Mapped[float | None] = mapped_column(Float, nullable=True)
    clip_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    period_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("processing_jobs.id", ondelete="SET NULL"),
        nullable=True,
    )
    is_acknowledged: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    acknowledged_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # "Actioned" is distinct from "acknowledged" (Issue #137): a coach marks an
    # alert actioned once they have turned it into a practice/scheme follow-up,
    # so the staff can track which tendency breaks have been addressed.
    is_actioned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    actioned_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    actioned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    player: Mapped["Player | None"] = relationship("Player")
    clip: Mapped["Clip | None"] = relationship("Clip")
    acknowledger: Mapped["User | None"] = relationship("User", foreign_keys=[acknowledged_by])
    actioner: Mapped["User | None"] = relationship("User", foreign_keys=[actioned_by])


class ActiveLearningQueueItem(Base):
    """Queue entry for low-confidence, regressed, or hard-negative samples."""

    __tablename__ = "active_learning_queue"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    clip_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clips.id", ondelete="SET NULL"), nullable=True, index=True
    )
    label_type: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    queue_reason: Mapped[ActiveLearningReason] = mapped_column(
        Enum(ActiveLearningReason, name="active_learning_reason"),
        nullable=False,
    )
    priority_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    model_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    source_model_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("model_versions.id", ondelete="SET NULL"),
        nullable=True,
    )
    correction_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("coach_corrections.id", ondelete="SET NULL"),
        nullable=True,
    )
    status: Mapped[ActiveLearningStatus] = mapped_column(
        Enum(ActiveLearningStatus, name="active_learning_status"),
        nullable=False,
        default=ActiveLearningStatus.queued,
    )
    metadata_: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# ── Phase 3: learned play embeddings (Issue #8, design in #77) ────────────────


# Embedding-vector dimensions are pinned to the design doc shape so the
# pgvector column DDL, the fusion arithmetic in ``stage_embed`` and the
# similarity search router all agree on the same numbers without having to
# read schema metadata at runtime.
PLAY_EMBEDDING_DIM: int = 256
PLAY_EMBEDDING_VISUAL_DIM: int = 192
PLAY_EMBEDDING_STRUCTURED_DIM: int = 64
# Raw CLIP ViT-B/32 image embedding, in CLIP's shared text-image space
# (Issue #195). Stored *unprojected* so a CLIP text-tower query can be
# cosine-compared against it — the fused ``vector`` above is not in CLIP
# space (its visual half is a random-init projection per
# ``docs/embeddings-architecture.md`` §7), so it cannot serve text search.
PLAY_EMBEDDING_CLIP_DIM: int = 512


class PlayEmbedding(Base):
    """A learned 256-d embedding describing one play (or play sub-chunk).

    The primary retrievable vector is the fused ``vector`` column; the
    sub-embeddings are retained so the retrieval router can re-weight
    structured vs. visual contribution at query time without re-running
    the encoder. ``UNIQUE (clip_id, chunk_kind, model_version_id)`` is the
    upsert key.

    ``is_experimental`` defaults to True — embeddings only flip to False
    when a derived concept cluster has been reviewed and accepted by a
    coach via ``EmbeddingClusterProposal``.
    """

    __tablename__ = "playembeddings"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    clip_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clips.id", ondelete="CASCADE"), nullable=False, index=True
    )
    chunk_kind: Mapped[str] = mapped_column(String(20), nullable=False, default="play")
    snap_anchor: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # Retrievable fused vector + retained sub-embeddings.
    vector: Mapped[list[float]] = mapped_column(Vector(PLAY_EMBEDDING_DIM), nullable=False)
    visual_vector: Mapped[list[float] | None] = mapped_column(
        Vector(PLAY_EMBEDDING_VISUAL_DIM), nullable=True
    )
    structured_vector: Mapped[list[float] | None] = mapped_column(
        Vector(PLAY_EMBEDDING_STRUCTURED_DIM), nullable=True
    )
    # Raw CLIP ViT-B/32 image embedding (512-d, CLIP shared text-image
    # space) retained for genuine CLIP text-tower zero-shot search
    # (Issue #195). Nullable: populated only when a real CLIP visual
    # encoder ran (the default ``ZeroVisualEncoder`` leaves it NULL), and
    # backfilled incrementally by the nightly embed stage.
    clip_vector: Mapped[list[float] | None] = mapped_column(
        Vector(PLAY_EMBEDDING_CLIP_DIM), nullable=True
    )

    # Lineage
    model_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("model_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    calibration_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("field_calibrations.id", ondelete="SET NULL"),
        nullable=True,
    )
    # IDs of ``labels`` rows that fed the structured encoder; lets us
    # target-re-embed only clips whose labels have since been corrected.
    source_label_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False, default=list
    )
    used_sam_masks: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Quality / governance
    embedding_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_experimental: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("processing_jobs.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    clip: Mapped["Clip"] = relationship("Clip")
    model_version: Mapped["ModelVersion"] = relationship("ModelVersion")


class EmbeddingClusterProposal(Base):
    """An experimental concept cluster surfaced from playembeddings.

    Produced by an offline clustering job over ``playembeddings.vector``
    (HDBSCAN in v1). Each row is one cluster; ``member_clip_ids`` lists
    the clips that fell into it.  Proposals are coach-reviewed: an
    ``accept`` flips affiliated embeddings to ``is_experimental=False``
    and is expected to be followed by ``coach_corrections`` rows for the
    member clips.  A ``reject`` hides the proposal from further review.

    All proposals carry ``status='pending'`` until reviewed, and nothing
    in this table is ever surfaced on production dashboards — it lives
    behind the coach review surface only.
    """

    __tablename__ = "embedding_cluster_proposals"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    model_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("model_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    # Cluster discriminator — usually the HDBSCAN cluster label as a string,
    # so a single batch produces ``"0"``, ``"1"``, ... unique within the run.
    cluster_label: Mapped[str] = mapped_column(String(64), nullable=False)
    # Working name the discovery job assigned (e.g. "embedding_cluster_3");
    # coach renames it on accept.
    proposed_name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    member_clip_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False, default=list
    )
    # Cluster centroid in the same 256-d space as PlayEmbedding.vector.
    centroid: Mapped[list[float] | None] = mapped_column(Vector(PLAY_EMBEDDING_DIM), nullable=True)
    member_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cohesion_score: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Review workflow
    # status: "pending" | "accepted" | "rejected"
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", index=True)
    # is_experimental stays True until an accept also exports it as a
    # coach correction; the search router relies on this flag to keep
    # cluster output out of production results.
    is_experimental: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    review_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # On accept, the name the coach chose for the concept (e.g. "mesh-like
    # RPO read"). Null while pending.
    accepted_label_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    model_version: Mapped["ModelVersion"] = relationship("ModelVersion")
    reviewer: Mapped["User | None"] = relationship("User", foreign_keys=[reviewed_by])


class PlayerVisibilityAudit(Base):
    """Append-only audit log for player visibility state changes (Issue #114).

    Recorded on every transition by the visibility router so we can later show
    a coach who approved a player profile for recruiting and when.  Only state
    transitions and the actor are stored — never sensitive content.
    """

    __tablename__ = "player_visibility_audit"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    player_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("players.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    previous_state: Mapped[PlayerVisibilityState | None] = mapped_column(
        Enum(PlayerVisibilityState, name="player_visibility_state"), nullable=True
    )
    next_state: Mapped[PlayerVisibilityState] = mapped_column(
        Enum(PlayerVisibilityState, name="player_visibility_state"), nullable=False
    )
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# ── Player development passport (Issue #7) ─────────────────────────────────────


class PlayerProfile(Base):
    """Per-player, per-season development profile — the passport home record.

    One row per ``(player, season)``. The outward-facing visibility lifecycle
    is governed entirely by the parent :class:`Player.visibility_state`
    (Issue #114); this table deliberately does **not** duplicate that state so
    there is a single source of truth. ``coach_notes`` is private staff content
    and is stripped from every non-staff projection by
    :func:`app.governance.shape_player_profile` — never serialize it directly.
    """

    __tablename__ = "player_profiles"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    player_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("players.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Season label, e.g. "2026". Free-form string so spring/fall splits or
    # custom training blocks can reuse the table without a schema change.
    season: Mapped[str] = mapped_column(String(16), nullable=False)
    # Staff-facing role description (e.g. "slot receiver / gunner").
    role_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Position-specific development goals, configurable per position group.
    development_goals: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # PRIVATE — coaches/analysts only. Never exposed to player/recruiting views.
    coach_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Player-facing summary, written/approved by staff.
    player_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Benchmark cohort: "position_group" | "class_year" | "role".
    benchmark_group: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # Curated best teaching / recruiting clip ids (list of uuid strings).
    favorite_clip_ids: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    generated_by: Mapped[ProfileGenerationSource] = mapped_column(
        Enum(ProfileGenerationSource, name="profile_generation_source"),
        nullable=False,
        default=ProfileGenerationSource.manual,
        server_default=ProfileGenerationSource.manual.value,
    )
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Per-player overrides of the canonical health-context field→tier mapping
    # (Issue #9): {"field_or_category": "workload" | "rehab" | "medical"}.
    # Overrides may only ESCALATE a field to a stricter tier — they can never
    # widen access (enforced by app.governance.effective_field_tier).
    restricted_context_flags: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint("player_id", "season", name="player_profiles_player_season_uq"),
    )

    player: Mapped["Player"] = relationship("Player")
    snapshots: Mapped[list["PlayerProfileSnapshot"]] = relationship(
        "PlayerProfileSnapshot", back_populates="profile", cascade="all, delete-orphan"
    )


class PlayerProfileSnapshot(Base):
    """Weekly development snapshot for a player profile (Issue #7).

    Captures the longitudinal record: per-week summary metrics, strengths,
    development focus, and evidence clips, alongside a confidence-scored
    identity resolution. A snapshot only participates in player-facing or
    recruiting projections once ``approval_status == approved``; low-confidence
    or unknown identities are forced ``experimental_flag = True`` and cannot be
    approved (see ``app.routers.player_profiles``).
    """

    __tablename__ = "player_profile_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    profile_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("player_profiles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Denormalized parent player id so snapshot queries don't have to JOIN
    # player_profiles to filter/authorize by player.
    player_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("players.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # The Monday (or chosen anchor) of the snapshot week.
    week_start: Mapped[Any] = mapped_column(Date, nullable=False, index=True)
    summary_metrics: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    strengths: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    development_focus: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    evidence_clip_ids: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    # ── Confidence-scored identity (Issue #7 — MP4-only, no face recognition) ──
    identity_state: Mapped[PlayerIdentityState] = mapped_column(
        Enum(PlayerIdentityState, name="player_identity_state"),
        nullable=False,
        default=PlayerIdentityState.needs_review,
        server_default=PlayerIdentityState.needs_review.value,
    )
    identity_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Source signals that produced the identity, e.g.
    # {"jersey_ocr": 0.4, "appearance": 0.6, "trajectory": 0.7,
    #  "roster_mapping": true, "manual_correction": true}. Never face data.
    identity_signals: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    generated_by: Mapped[ProfileGenerationSource] = mapped_column(
        Enum(ProfileGenerationSource, name="profile_generation_source", create_type=False),
        nullable=False,
        default=ProfileGenerationSource.model_assisted,
        server_default=ProfileGenerationSource.model_assisted.value,
    )
    approval_status: Mapped[SnapshotApprovalStatus] = mapped_column(
        Enum(SnapshotApprovalStatus, name="snapshot_approval_status"),
        nullable=False,
        default=SnapshotApprovalStatus.pending,
        server_default=SnapshotApprovalStatus.pending.value,
    )
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Forced True when identity confidence is below threshold or the identity
    # state is not known/probable — such a snapshot is never silently attached
    # to a player-facing profile.
    experimental_flag: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    profile: Mapped["PlayerProfile"] = relationship("PlayerProfile", back_populates="snapshots")


# ── Player workload rollups (Issue #149) ─────────────────────────────────────


class PlayerWorkloadDaily(Base):
    """Nightly per-player workload rollup for the health/workload surface.

    One row per (player, day), written by the nightly ``workload_rollup``
    job: summed CV daily load, sprint count, max speed, confidence-weighted
    gait asymmetry, the trailing 7d/28d acute:chronic workload ratio, and the
    experimental heuristic risk score. Rows are only ever written for
    identity-confident tracklets (>= 0.70); low-confidence contributions stay
    per-clip as anonymous-track metric rows and never roll into a named
    player-day. All values are experimental sports-performance indicators —
    never a diagnosis — and are read exclusively through the RBAC-gated,
    audit-logged ``/api/v1/health-workload/*`` endpoints.
    """

    __tablename__ = "player_workload_daily"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    player_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("players.id", ondelete="CASCADE"), nullable=False, index=True
    )
    date: Mapped[Any] = mapped_column(Date, nullable=False, index=True)
    # Summed CV distance_traveled (yards) across the day's clips.
    daily_load: Mapped[float | None] = mapped_column(Float, nullable=True)
    sprint_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_speed_yps: Mapped[float | None] = mapped_column(Float, nullable=True)
    asymmetry_index: Mapped[float | None] = mapped_column(Float, nullable=True)
    acute_load_7d: Mapped[float | None] = mapped_column(Float, nullable=True)
    chronic_load_28d: Mapped[float | None] = mapped_column(Float, nullable=True)
    acwr: Mapped[float | None] = mapped_column(Float, nullable=True)
    injury_risk_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    risk_reason_codes: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    clip_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    attribution: Mapped[str] = mapped_column(
        String(20), nullable=False, default="player", server_default="player"
    )
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Model-router variant that produced the risk score (#73 audit trail).
    model_variant: Mapped[str | None] = mapped_column(String(50), nullable=True)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    player: Mapped["Player"] = relationship("Player")

    __table_args__ = (UniqueConstraint("player_id", "date", name="player_workload_daily_unique"),)


# ── Health data-fusion tables (Issue #9) ─────────────────────────────────────
# UT-source joins for the athlete health/workload dashboard. All reads go
# through the RBAC-gated, audit-logged /api/v1/health-workload/* endpoints;
# writes go through the sportsperformance-gated ingest endpoints. Data is
# sports-performance context — never a clinical record.


class WellnessEntry(Base):
    """Daily athlete wellness self-report (workload tier).

    Self-reported context only — never a clinical assessment. One row per
    (player, day); 1–10 subjective scales plus sleep hours.
    """

    __tablename__ = "wellness_entries"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    player_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("players.id", ondelete="CASCADE"), nullable=False, index=True
    )
    date: Mapped[Any] = mapped_column(Date, nullable=False, index=True)
    soreness: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sleep_hours: Mapped[float | None] = mapped_column(Float, nullable=True)
    sleep_quality: Mapped[int | None] = mapped_column(Integer, nullable=True)
    energy: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stress: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source: Mapped[str] = mapped_column(
        String(50), nullable=False, default="manual_csv", server_default="manual_csv"
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (UniqueConstraint("player_id", "date", name="wellness_entries_unique"),)


class GpsWorkloadDaily(Base):
    """Daily GPS/wearable movement load (Catapult-style; workload tier)."""

    __tablename__ = "gps_workload_daily"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    player_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("players.id", ondelete="CASCADE"), nullable=False, index=True
    )
    date: Mapped[Any] = mapped_column(Date, nullable=False, index=True)
    session_type: Mapped[str] = mapped_column(
        String(30), nullable=False, default="practice", server_default="practice"
    )
    total_distance_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    high_speed_distance_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    sprint_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_speed_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    accel_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    decel_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    player_load: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(
        String(50), nullable=False, default="manual_csv", server_default="manual_csv"
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("player_id", "date", "session_type", name="gps_workload_daily_unique"),
    )


class ScSession(Base):
    """Strength & conditioning session load (workload tier)."""

    __tablename__ = "sc_sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    player_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("players.id", ondelete="CASCADE"), nullable=False, index=True
    )
    date: Mapped[Any] = mapped_column(Date, nullable=False, index=True)
    session_volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    tonnage_kg: Mapped[float | None] = mapped_column(Float, nullable=True)
    session_rpe: Mapped[float | None] = mapped_column(Float, nullable=True)
    duration_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source: Mapped[str] = mapped_column(
        String(50), nullable=False, default="manual_csv", server_default="manual_csv"
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (UniqueConstraint("player_id", "date", name="sc_sessions_unique"),)


class AcademicCalendarEvent(Base):
    """Team-level academic-stress overlay (exams, midterms, breaks, travel).

    Deliberately has NO per-player academic data — only calendar context that
    the dashboard overlays on workload trends.
    """

    __tablename__ = "academic_calendar_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    date: Mapped[Any] = mapped_column(Date, nullable=False, index=True)
    end_date: Mapped[Any | None] = mapped_column(Date, nullable=True)
    event_type: Mapped[str] = mapped_column(String(30), nullable=False)
    label: Mapped[str | None] = mapped_column(String(200), nullable=True)
    source: Mapped[str] = mapped_column(
        String(50), nullable=False, default="manual_csv", server_default="manual_csv"
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class InjuryHistory(Base):
    """Coarse injury-history rows (rehab tier — restricted by default).

    NO diagnosis free-text by design: body region, side, coarse status, and
    days missed only. Restricted to the rehab tier's roles (admin +
    sportsperformance) at every read path.
    """

    __tablename__ = "injury_history"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    player_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("players.id", ondelete="CASCADE"), nullable=False, index=True
    )
    reported_date: Mapped[Any] = mapped_column(Date, nullable=False, index=True)
    body_region: Mapped[str] = mapped_column(String(50), nullable=False)
    side: Mapped[str | None] = mapped_column(String(10), nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="active", server_default="active"
    )
    days_missed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    restricted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true()
    )
    source: Mapped[str] = mapped_column(
        String(50), nullable=False, default="manual_csv", server_default="manual_csv"
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# ── Settings (Issue #112) ─────────────────────────────────────────────────────


class SystemSetting(Base):
    """System-wide configuration as flexible KV rows.

    One row per logical key (e.g. ``"system_config"``, ``"model_sensitivity"``).
    ``value`` is a JSON blob whose shape is validated by the Pydantic schemas
    in ``app.schemas.settings``. Reads always merge persisted values over the
    schema defaults so absent rows still produce a populated config response.
    """

    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class UserSetting(Base):
    """Per-user preference rows.

    Composite PK on ``(user_id, key)`` so a user can store any number of
    independent preference blobs. Schemas in ``app.schemas.settings`` define
    the known keys and their shapes.
    """

    __tablename__ = "user_settings"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


# ── Reports (Issue #111) ──────────────────────────────────────────────────────


class ReportJob(Base):
    """Async report export tracking.

    Deliberately separate from :class:`ProcessingJob` (which is wired to the
    GPU pipeline + Cloudflare Queues): report generation runs in-process via
    FastAPI ``BackgroundTasks`` and produces lightweight artifacts (PDF/CSV/
    JSON) uploaded to R2. Status reuses :class:`JobStatus` so the frontend can
    share status pill rendering across both job kinds.
    """

    __tablename__ = "report_jobs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    report_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    format: Mapped[ReportFormat] = mapped_column(
        Enum(ReportFormat, name="report_format"), nullable=False
    )
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, name="job_status", create_type=False),
        nullable=False,
        default=JobStatus.queued,
    )
    # Section selections + filters (date range, session kind, etc.)
    parameters: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # R2 URI of the generated artifact, populated on success.
    output_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    requester: Mapped["User | None"] = relationship("User", foreign_keys=[requested_by])


# ── Pre-snap prediction (Issues #135 / #136) ───────────────────────────────────


class PlayPrediction(Base):
    """A calibrated pre-snap run/pass prediction for a single snap.

    The six pre-snap signals (Issue #135) are stored verbatim in
    ``signal_vector``; the Bayesian ensemble outputs (Issue #136) — raw and
    calibrated probability, the combined logit, predicted/true class,
    confidence, and the calibrated uncertainty (Issue #146) — sit alongside so
    the MLOps dashboard and clip-review overlay can read one row per play.
    """

    __tablename__ = "play_predictions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    clip_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clips.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Plays are not their own table yet; ``play_id`` is an optional opaque key.
    play_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # Opponent identified by team name (matches the derived-opponent model).
    opponent_team: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    # The 6 pre-snap signals (Issue #135), each with its own confidence.
    signal_vector: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    logit_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    predicted_class: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Filled in by the coach-correction flywheel once the play is confirmed.
    true_class: Mapped[str | None] = mapped_column(String(32), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    calibrated_prob: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Calibrated uncertainty (binary entropy, bits) — Issue #146.
    uncertainty: Mapped[float | None] = mapped_column(Float, nullable=True)
    # False until a fitted Platt scaler produced ``calibrated_prob``.
    is_calibrated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    model_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("model_versions.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    clip: Mapped["Clip"] = relationship("Clip")


class OpponentPrior(Base):
    """Per-opponent situational Dirichlet (Beta) run/pass posterior.

    One row per (opponent, down, distance-bucket, field-zone). The default
    ``Beta(2,2)`` is uninformative (P(pass)=0.5); a cell only diverges once
    coach-confirmed outcomes accumulate (``n_pass`` / ``n_run``), so every
    non-default prior is data-backed — there are no hard-coded opponent priors.
    """

    __tablename__ = "opponent_priors"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    opponent_team: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    down: Mapped[int | None] = mapped_column(Integer, nullable=True)
    distance_bucket: Mapped[str | None] = mapped_column(String(16), nullable=True)
    field_zone: Mapped[str | None] = mapped_column(String(16), nullable=True)
    alpha_pass: Mapped[float] = mapped_column(Float, nullable=False, default=2.0)
    alpha_run: Mapped[float] = mapped_column(Float, nullable=False, default=2.0)
    n_pass: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    n_run: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "opponent_team",
            "down",
            "distance_bucket",
            "field_zone",
            name="opponent_priors_situation_uq",
        ),
    )


# ── CFBD cache tables (Issues #160/#161/#162) ──────────────────────────────────
# Imported here so the College Football Data cache tables register with
# ``Base.metadata`` (Alembic autogenerate + the test suite see them) without
# bloating this module. The models themselves live in ``app.cfbd.models``.
from app.cfbd.models import (  # noqa: E402,F401
    CFBDDrive,
    CFBDGame,
    CFBDPlay,
    CFBDSyncRun,
    CFBDSyncStatus,
    CFBDTeam,
    CFBDTeamGameStat,
    CFBDWinProbability,
)

# Backward-compatible aliases used by the read-only analytics router/tests from
# Issue #163. The canonical ORM classes now live in ``app.cfbd.models``.
CfbdSyncStatus = CFBDSyncStatus
CfbdSyncRun = CFBDSyncRun
CfbdTeamRow = CFBDTeam
CfbdGame = CFBDGame
CfbdDrive = CFBDDrive
CfbdPlay = CFBDPlay
CfbdTeamGameStat = CFBDTeamGameStat
CfbdWinProbability = CFBDWinProbability


# ── Phase 4: playbook overlays & assignment scoring (Issue #15) ────────────────


class PlaybookConcept(Base):
    """A coach-authored call-sheet concept and its assignment definitions.

    ``side`` is "offense" or "defense"; ``assignments`` is the JSON list of
    assignment dicts (key, role, intent, coaching_point, intended_route, rule)
    that drive both the Film Room overlay and rule-based execution scoring. The
    table is intentionally light and coach-edited — see ``app.playbook_seed``
    for the two starter concepts and ``docs/playbook-overlays.md`` for the
    contract.
    """

    __tablename__ = "playbook_concepts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    side: Mapped[str] = mapped_column(String(16), nullable=False)
    structure_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # List of assignment definition dicts (see app.analytics.assignment_scoring).
    assignments: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    coaching_points: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true()
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class PlayConcept(Base):
    """Links a concept to the clip (play) on which it was called.

    ``source`` is "coach" (coach confirmed the call) or "model" (auto-identified
    and therefore unvalidated). ``assignment_player_map`` maps each assignment
    key to ``{"tracklet_id": ..., "identity_state": ..., "identity_confidence": ...}``
    so scoring can resolve which tracklet ran each assignment without requiring
    face recognition. ``validated`` records whether a coach confirmed the
    concept identification.
    """

    __tablename__ = "play_concepts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    clip_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clips.id", ondelete="CASCADE"), nullable=False, index=True
    )
    concept_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("playbook_concepts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, default="coach", server_default="coach"
    )
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    assignment_player_map: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    validated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("clip_id", "concept_id", name="play_concepts_clip_concept_uq"),
    )

    concept: Mapped["PlaybookConcept"] = relationship("PlaybookConcept")


class AssignmentScore(Base):
    """A rule-based execution grade for one assignment on one play (Issue #15).

    ``grade`` is on_assignment / off_assignment / needs_review. A low-confidence
    identity or missing substrate yields ``needs_review`` (never a negative
    grade). ``coach_grade`` + ``coach_comment`` capture a coach override, which
    also writes an ``assignment_execution`` Label so corrections feed the
    training flywheel. ``experimental`` defaults true until validated.
    """

    __tablename__ = "assignment_scores"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    clip_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clips.id", ondelete="CASCADE"), nullable=False, index=True
    )
    concept_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("playbook_concepts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    assignment_key: Mapped[str] = mapped_column(String(64), nullable=False)
    grade: Mapped[str] = mapped_column(String(24), nullable=False)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    confidence: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0, server_default="0"
    )
    uncertainty: Mapped[float] = mapped_column(
        Float, nullable=False, default=1.0, server_default="1"
    )
    reason_codes: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    # Snapshot of the signal confidences that produced the grade.
    inputs: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    experimental: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true()
    )
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="auto", server_default="auto"
    )
    coach_grade: Mapped[str | None] = mapped_column(String(24), nullable=True)
    coach_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    overridden_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    overridden_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "clip_id", "concept_id", "assignment_key", name="assignment_scores_unique"
        ),
    )
