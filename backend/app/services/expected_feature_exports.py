"""Bounded feature export intake/signing; never model inference or target authoring."""

from __future__ import annotations

import hashlib
import json
import math
import time
import uuid
from typing import Any

from anyio import to_thread
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Clip, JobStatus, JobType, PlayView, ProcessingJob, Video
from app.services.expected_outcomes import FEATURE_VERSION, _hash, _read
from app.services.media_identity import verified_job_media_identity


async def feature_export_download(
    db: AsyncSession, job_id: uuid.UUID, *, include_content: bool = False
) -> dict[str, Any]:
    from app.config import get_settings
    from app.services.annotations import annotation_revision
    from app.services.clip_state import clip_is_active
    from app.storage import sign_stream_path

    job = await db.get(ProcessingJob, job_id)
    if job is None or job.job_type != JobType.pipeline or job.status != JobStatus.succeeded:
        raise HTTPException(404, "Completed pipeline feature export not found")
    descriptor = (job.output_artifacts or {}).get("expected_feature_export")
    if not isinstance(descriptor, dict):
        raise HTTPException(404, "No qualified expected feature snapshots were exported")
    try:
        video = await db.get(Video, job.video_id)
        if video is None:
            raise ValueError("Export recording is unavailable")
        attested = await verified_job_media_identity(db, job)
        if attested is None:
            raise ValueError("Export lacks this job's trusted fenced source attestation")
        digest = _hash(descriptor["sha256"])
        uri = descriptor["artifact_uri"]
        from app.storage import ARTIFACTS, is_valid_key, parse_storage_uri

        _scheme, bucket, key = parse_storage_uri(uri)
        if (
            bucket != ARTIFACTS
            or not is_valid_key(key)
            or key != f"training_features/expected_outcomes/{video.id}/{digest}.json"
        ):
            raise ValueError("Export must use its owned content-addressed feature artifact")
        document = await to_thread.run_sync(_read, uri, digest)
        media = attested["full_sha256"]
        expected = {
            "schema_version": 1,
            "kind": "expected_outcome_feature_export",
            "source": "real",
            "video_id": str(video.id),
            "job_id": str(job.id),
            "source_uri": video.storage_uri,
            "media_sha256": media,
            "source_clock": "verified_native_pts",
            "targets_included": False,
            "review_attestations_filled": False,
        }
        if not media or any(
            type(document.get(key)) is not type(value) or document.get(key) != value
            for key, value in expected.items()
        ):
            raise ValueError("Export source/job/native-clock identity differs")
        rows = document["feature_snapshots"]
        if (
            not isinstance(rows, dict)
            or not rows
            or type(descriptor["row_count"]) is not int
            or len(rows) != descriptor["row_count"]
            or len(rows) > 10000
        ):
            raise ValueError("Export snapshot count differs")
        clips = {}
        for identity, row in rows.items():
            clip_id, task = identity.split(":", 1)
            clip_uuid = uuid.UUID(clip_id)
            if clip_id not in clips:
                clip = await db.get(Clip, clip_uuid)
                canonical = (
                    await db.execute(select(PlayView.play_id).where(PlayView.clip_id == clip_uuid))
                ).scalar_one_or_none() or clip_uuid
                clips[clip_id] = clip, canonical
            clip, canonical = clips[clip_id]
            lineage, clock = row["lineage"], row["observation_clock"]["timestamp_seconds"]
            scope = document.get("clip_revisions", {}).get(clip_id)
            if (
                clip is None
                or not clip_is_active(clip)
                or not isinstance(scope, dict)
                or any(type(value) is not int or value < 0 for value in scope.values())
                or scope
                != {
                    "annotation_revision": annotation_revision(clip),
                    "observation_revision": (clip.label_data or {}).get("_observation_revision", 0),
                }
                or clip.video_id != video.id
                or row["task"] != task
                or row["source"] != "real"
                or row["feature_version"] != FEATURE_VERSION
                or "target" in row
                or "split" in row
                or not isinstance(clock, (int, float))
                or isinstance(clock, bool)
                or not math.isfinite(clock)
                or not clip.start_time <= clock <= clip.end_time
                or any(
                    lineage.get(key) != value
                    for key, value in {
                        "recording_id": str(video.id),
                        "session_id": str(video.practice_session_id),
                        "canonical_play_id": str(canonical),
                        "media_sha256": media,
                    }.items()
                )
            ):
                raise ValueError("Export snapshot is foreign, changed or outside its source clock")
        encoded = json.dumps(
            document, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
        if hashlib.sha256(encoded).hexdigest() != digest:
            raise ValueError("Export must retain its exact canonical artifact bytes")
        if type(descriptor["size_bytes"]) is not int or descriptor["size_bytes"] != len(encoded):
            raise ValueError("Export byte count differs")
        exp = int(time.time()) + get_settings().s3_presign_ttl
        sig = sign_stream_path("expected-feature-export", f"{job.id}/{digest}", exp)
        return {
            **descriptor,
            "download_url": (
                f"/api/v1/learning/expected-outcomes/features/{job.id}/file?exp={exp}&sig={sig}"
            ),
            "filename": f"expected-features-{job.id}.json",
            **({"content": encoded} if include_content else {}),
        }
    except (KeyError, ValueError, TypeError, AttributeError, OSError) as exc:
        raise HTTPException(422, f"Expected feature export refused: {exc}") from exc
