"""Actual label removal fences downstream snapshots and invalidates observations."""

# ruff: noqa: F811
import uuid
from datetime import UTC, datetime

import pytest
from app.models import CoachCorrection, Label, Metric
from app.services.annotations import annotation_revision
from sqlalchemy import select
from test_clip_edits import film, workspace  # noqa: F401


@pytest.mark.parametrize("source", ["human", "manual", "model"])
async def test_delete_reviewed_label_invalidates_revision_cache_and_metrics(workspace, source):
    client, db, session = workspace
    _, clips = await film(db, session, [(0, 4)])
    clip = clips[0]
    clip.label_data = {**clip.label_data, "coverage_shell": {"coverage": "cover_1"}}
    clip.is_reviewed = True
    row = Label(
        id=uuid.uuid4(),
        clip_id=clip.id,
        label_type="coverage_shell",
        label_value={"coverage": "cover_1"},
        source=source,
    )
    metric = Metric(
        id=uuid.uuid4(),
        clip_id=clip.id,
        metric_name="observed_test_metric",
        metric_value={"value": 1},
        analytics_safe=True,
        is_suppressed=False,
    )
    db.add_all([row, metric])
    await db.flush()
    previous = annotation_revision(clip)
    result = await client.delete(f"/api/v1/labels/{row.id}")
    assert result.status_code == 204, result.text
    assert (await client.get(f"/api/v1/labels/{row.id}")).status_code == 404
    assert (await client.get(f"/api/v1/clips/{clip.id}/labels")).json() == []
    await db.refresh(metric)
    if source in {"human", "manual"}:
        assert annotation_revision(clip) == previous + 1
        assert "coverage_shell" not in clip.label_data and clip.is_reviewed is False
        assert metric.is_suppressed is True and metric.analytics_safe is False
        audit = (
            await db.execute(select(CoachCorrection).where(CoachCorrection.clip_id == clip.id))
        ).scalar_one()
        assert audit.training_eligible is False and audit.corrected_value[
            "removed_label_id"
        ] == str(row.id)
        # Deleting twice neither advances revision nor reintroduces observations.
        assert (await client.delete(f"/api/v1/labels/{row.id}")).status_code == 404
        assert annotation_revision(clip) == previous + 1
    else:
        assert annotation_revision(clip) == previous and metric.is_suppressed is False


async def test_clip_label_pagination_has_uuid_tiebreaker(workspace):
    client, db, session = workspace
    _, clips = await film(db, session, [(0, 4)])
    clip = clips[0]
    identities = [uuid.UUID(int=value) for value in (36, 12, 30, 18, 24, 6)]
    timestamp = datetime(2026, 10, 21, tzinfo=UTC)
    db.add_all(
        [
            Label(
                id=identity,
                clip_id=clip.id,
                label_type="protocol",
                label_value={},
                source="heuristic",
                created_at=timestamp,
            )
            for identity in identities
        ]
    )
    await db.flush()
    pages = []
    for offset in (0, 2, 4):
        response = await client.get(
            f"/api/v1/clips/{clip.id}/labels", params={"limit": 2, "offset": offset}
        )
        assert response.status_code == 200, response.text
        pages.extend(row["id"] for row in response.json())
    assert pages == [str(identity) for identity in sorted(identities)]


async def test_locked_delete_refreshes_source_changed_since_initial_read(workspace):
    from sqlalchemy import update

    client, db, session = workspace
    _, clips = await film(db, session, [(0, 4)])
    clip = clips[0]
    row = Label(
        id=uuid.uuid4(),
        clip_id=clip.id,
        label_type="coverage_shell",
        label_value={"coverage": "cover_1"},
        source="model",
    )
    db.add(row)
    await db.flush()
    # Reproduce the identity-map state of a request waiting behind a correction.
    await db.execute(
        update(Label)
        .where(Label.id == row.id)
        .values(source="human")
        .execution_options(synchronize_session=False)
    )
    assert row.source == "model"
    response = await client.delete(f"/api/v1/labels/{row.id}")
    assert response.status_code == 204, response.text
    assert annotation_revision(clip) == 1
    audit = (
        await db.execute(select(CoachCorrection).where(CoachCorrection.clip_id == clip.id))
    ).scalar_one()
    assert audit.original_value["removed_label"]["source"] == "human"
