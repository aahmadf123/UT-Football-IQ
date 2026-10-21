"""Reviewed temporal evidence is unique, source-bounded and same-shot."""

import uuid

import pytest
from app.models import Event
from app.services.metric_eligibility import automatic_exclusion
from test_platform_overhaul import db, recording  # noqa: F401


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "snap_duplicate",
        "release_alias",
        "shot",
        "unknown_shot",
        "unknown_clock",
        "contradictory_clock",
        "outside",
        "packet_clock",
        "missing_clock",
        "unresolved_snap",
        "conflicting_shot",
        "zero_duration",
    ],
)
async def test_ttt_rejects_ambiguous_or_unknown_source_clocks(db, fault):  # noqa: F811
    _, clip = await recording(db)
    clip.start_time, clip.end_time = 100, 105
    snap = Event(
        id=uuid.uuid4(),
        clip_id=clip.id,
        event_type="snap",
        timestamp_seconds=1,
        attributes={"timebase": "clip", "shot_id": 0},
        provenance={"source": "human"},
    )
    release = Event(
        id=uuid.uuid4(),
        clip_id=clip.id,
        event_type="pass_release",
        timestamp_seconds=104,
        attributes={"timebase": "recording", "shot_id": "000"},
        provenance={"source": "human"},
    )
    if fault == "snap_duplicate":
        db.add(
            Event(
                clip_id=clip.id,
                event_type="snap",
                timestamp_seconds=1.2,
                attributes={"timebase": "clip", "shot_id": 0},
                provenance={"source": "human"},
            )
        )
    if fault == "release_alias":
        db.add(
            Event(
                clip_id=clip.id,
                event_type="throw",
                timestamp_seconds=103,
                attributes={"timebase": "recording", "shot_id": 0},
                provenance={"source": "human"},
            )
        )
    if fault == "shot":
        release.attributes["shot_id"] = "other"
    if fault == "unknown_shot":
        snap.attributes["shot_id"] = "unknown"
    if fault == "unknown_clock":
        snap.attributes["timebase"] = ["recording"]
    if fault == "contradictory_clock":
        snap.provenance["timebase"] = "recording"
    if fault == "outside":
        snap.timestamp_seconds = 10
    if fault == "missing_clock":
        snap.attributes.pop("timebase")
    if fault == "unresolved_snap":
        db.add(
            Event(
                clip_id=clip.id,
                event_type="snap",
                timestamp_seconds=None,
                attributes={"timebase": "clip", "shot_id": 0},
                provenance={"source": "human"},
            )
        )
    if fault == "conflicting_shot":
        release.provenance["shot_id"] = "other"
    if fault == "zero_duration":
        release.timestamp_seconds = 101
    db.add_all([snap, release])
    await db.flush()
    reason = await automatic_exclusion(
        db,
        clip=clip,
        name="time_to_throw",
        value={
            "seconds": 0 if fault == "zero_duration" else 3,
            **({"source_start_seconds": 1} if fault == "packet_clock" else {}),
        },
        provenance={"validation_source": "human_event_timestamps"},
        model_id=None,
    )
    assert (reason is None) is (fault is None)
