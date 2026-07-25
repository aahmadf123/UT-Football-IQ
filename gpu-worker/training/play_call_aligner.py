"""Pair practice <-> game clips by coach-tagged play-call id (Issue #150 §5.14).

Toledo runs the same play in practice (``drone_follow`` — the hard vision regime,
where 90%+ of footage is generated) and in a game (``fixed_sideline`` — the easy
regime: static, full field, larger players). Coaching staff tag both clips with
the *same* ``play_call_id`` (see ``clips.play_call_id``, Issue #150 migration
0026). This module groups clips by that key and builds an **auditable paired-play
manifest** that the cross-regime self-distillation trainer
(:mod:`training.cross_regime_distill`) consumes: the high-quality game pipeline
output becomes the *teacher* and is distilled onto the *student* practice clip.

Hard two-regime guarantee (owner constraint on the issue): the teacher side must
be ``fixed_sideline`` and the student side ``drone_follow``. A play-call group
that is missing one regime, has duplicate regimes, or is mislabeled is skipped
and logged — a paired play can never silently invert the two-regime design.

This module is intentionally dependency-light (stdlib + structlog): it operates
on clip-metadata dicts (as returned by
``GET /api/v1/videos/{video_id}/clips`` /
``backend.fetch_clips_for_pairing``), never the DB or torch, mirroring the
gpu-worker "consume payloads, don't hit the DB directly" convention.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog

log = structlog.get_logger(__name__)

# Regime string constants mirrored locally so this module stays truly
# dependency-light (importing pipeline.homography.regime_detector would pull
# in numpy at import time; these string literals never change independently).
DRONE_FOLLOW = "drone_follow"
FIXED_SIDELINE = "fixed_sideline"

# Bump when the manifest record shape changes. Stamped into every manifest.
SCHEMA_VERSION = 1

# Stamped onto the pairing manifest so a consumer can never mistake these for
# BDB offline records (``offline-pretraining-evaluation-only``) or for raw,
# unvalidated predictions. These are real Toledo paired practice/game examples.
USAGE_MARKER = "toledo-paired-cross-regime"

# Acceptance criterion: a usable distillation dataset needs >= 100 paired plays.
MIN_PAIRED_PLAYS = 100

# Correction-source vocabulary (owner constraint: distillation consumes
# corrected/validated examples, not raw uncertain predictions).
CORRECTION_HUMAN = "human"
CORRECTION_MODEL = "model"
CORRECTION_NONE = "none"

# Validation-status vocabulary, derived from ``clips.result_state`` (#147) and
# ``clips.is_reviewed``.
VALIDATION_FINAL = "final"
VALIDATION_PRELIMINARY = "preliminary"
VALIDATION_REVIEWED = "reviewed"
VALIDATION_UNKNOWN = "unknown"


@dataclass(frozen=True)
class ClipRef:
    """One side of a paired play — the auditable per-clip manifest record.

    Carries every field the issue's acceptance criteria require the manifest to
    preserve: video id, clip id, play-call id, capture regime, player/track
    identity confidence, correction source, validation status (plus the model
    version lineage that produced the clip's labels).
    """

    clip_id: str
    video_id: str | None
    play_call_id: str
    capture_regime: str
    identity_confidence: float | None
    correction_source: str
    validation_status: str
    model_version_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "clip_id": self.clip_id,
            "video_id": self.video_id,
            "play_call_id": self.play_call_id,
            "capture_regime": self.capture_regime,
            "identity_confidence": self.identity_confidence,
            "correction_source": self.correction_source,
            "validation_status": self.validation_status,
            "model_version_id": self.model_version_id,
        }


@dataclass(frozen=True)
class PairedPlay:
    """A game (teacher) clip aligned to the practice (student) clip of the same
    play. ``teacher`` is always ``fixed_sideline``; ``student`` always
    ``drone_follow``."""

    play_call_id: str
    teacher: ClipRef  # fixed_sideline game clip → pseudo-label source
    student: ClipRef  # drone_follow practice clip → distillation target

    @property
    def is_validated(self) -> bool:
        """True when both sides are coach-validated/corrected, not raw
        preliminary predictions (owner constraint)."""
        return _is_validated(self.teacher) and _is_validated(self.student)

    def to_dict(self) -> dict[str, Any]:
        return {
            "play_call_id": self.play_call_id,
            "teacher": self.teacher.to_dict(),
            "student": self.student.to_dict(),
            "is_validated": self.is_validated,
        }


def _is_validated(ref: ClipRef) -> bool:
    return ref.validation_status in (VALIDATION_FINAL, VALIDATION_REVIEWED) or (
        ref.correction_source == CORRECTION_HUMAN
    )


def _derive_correction_source(clip: dict[str, Any]) -> str:
    """Map a clip's correction metadata to the manifest vocabulary.

    Accepts either a precomputed ``correction_source`` field or a ``corrections``
    list (each entry carrying a ``source``/``correction_type``); falls back to
    the label ``source`` (``human``/``model``) when present.
    """
    explicit = clip.get("correction_source")
    if isinstance(explicit, str) and explicit:
        return explicit
    corrections = clip.get("corrections")
    if isinstance(corrections, list) and corrections:
        return CORRECTION_HUMAN
    source = clip.get("label_source") or clip.get("source")
    if source == CORRECTION_HUMAN:
        return CORRECTION_HUMAN
    if source == CORRECTION_MODEL:
        return CORRECTION_MODEL
    return CORRECTION_NONE


def _derive_validation_status(clip: dict[str, Any]) -> str:
    """Map ``result_state`` (#147) + ``is_reviewed`` to a validation status."""
    result_state = clip.get("result_state")
    if result_state in (VALIDATION_FINAL, VALIDATION_PRELIMINARY):
        return str(result_state)
    if clip.get("is_reviewed"):
        return VALIDATION_REVIEWED
    return VALIDATION_UNKNOWN


def _to_ref(clip: dict[str, Any], play_call_id: str) -> ClipRef:
    identity = clip.get("identity_confidence")
    if identity is None:
        identity = clip.get("track_confidence", clip.get("confidence"))
    return ClipRef(
        clip_id=str(clip.get("id") or clip.get("clip_id") or ""),
        video_id=(str(clip["video_id"]) if clip.get("video_id") is not None else None),
        play_call_id=play_call_id,
        capture_regime=str(clip.get("capture_regime") or ""),
        identity_confidence=(float(identity) if identity is not None else None),
        correction_source=_derive_correction_source(clip),
        validation_status=_derive_validation_status(clip),
        model_version_id=(
            str(clip["model_version_id"]) if clip.get("model_version_id") is not None else None
        ),
    )


def align_plays(clips: list[dict[str, Any]]) -> list[PairedPlay]:
    """Group clips by ``play_call_id`` and pair the game clip with the practice
    clip of the same play.

    Clips with a null/blank ``play_call_id`` are ignored (unpaired). A group is
    only emitted when it has exactly one ``fixed_sideline`` clip (teacher) *and*
    one ``drone_follow`` clip (student); any other shape (missing a regime,
    duplicate regimes, unknown regime) is skipped and logged so a mislabeled
    pair can never invert the two-regime design.
    """
    by_call: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for clip in clips:
        call_id = clip.get("play_call_id")
        if not call_id:
            continue
        by_call[str(call_id)].append(clip)

    pairs: list[PairedPlay] = []
    for call_id, group in sorted(by_call.items()):
        teachers = [c for c in group if c.get("capture_regime") == FIXED_SIDELINE]
        students = [c for c in group if c.get("capture_regime") == DRONE_FOLLOW]
        if len(teachers) != 1 or len(students) != 1:
            log.warning(
                "play_call_unpaired_skipped",
                play_call_id=call_id,
                n_fixed_sideline=len(teachers),
                n_drone_follow=len(students),
                group_size=len(group),
            )
            continue
        pairs.append(
            PairedPlay(
                play_call_id=call_id,
                teacher=_to_ref(teachers[0], call_id),
                student=_to_ref(students[0], call_id),
            )
        )
    log.info("play_calls_aligned", n_pairs=len(pairs), n_call_groups=len(by_call))
    return pairs


def filter_validated(pairs: list[PairedPlay]) -> list[PairedPlay]:
    """Keep only pairs where both sides are coach-validated/corrected.

    The distillation trainer uses this so self-distillation consumes
    corrected/validated examples rather than raw uncertain predictions.
    """
    kept = [p for p in pairs if p.is_validated]
    log.info("paired_plays_validated_filter", kept=len(kept), dropped=len(pairs) - len(kept))
    return kept


def meets_min_plays(pairs: list[PairedPlay], minimum: int = MIN_PAIRED_PLAYS) -> bool:
    """Whether enough paired plays exist to build the distillation dataset."""
    return len(pairs) >= minimum


def build_pairing_manifest(
    pairs: list[PairedPlay],
    *,
    source: str,
    minimum: int = MIN_PAIRED_PLAYS,
) -> dict[str, Any]:
    """Build the auditable paired-play manifest.

    Reuses the BDB provenance shape (usage marker, schema version, generated-at,
    record counts, explanatory notes). Every record preserves video id, clip id,
    play-call id, capture regime, identity confidence, correction source, and
    validation status for both the teacher (game) and student (practice) side.
    """
    n_validated = sum(1 for p in pairs if p.is_validated)
    return {
        "usage": USAGE_MARKER,
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "source": source,
        "teacher_regime": FIXED_SIDELINE,
        "student_regime": DRONE_FOLLOW,
        "record_counts": {
            "paired_plays": len(pairs),
            "validated_pairs": n_validated,
        },
        "min_paired_plays": minimum,
        "meets_min_plays": meets_min_plays(pairs, minimum),
        "notes": (
            "Teacher = fixed_sideline game clip (high-quality nightly pseudo-labels); "
            "student = drone_follow practice clip (distillation target). Pairs are matched "
            "by coach-tagged play_call_id. Regime metadata is preserved on every record so a "
            "drone_follow student detection can never be confused with a fixed_sideline "
            "teacher output (Issue #150 / #126)."
        ),
        "pairs": [p.to_dict() for p in pairs],
    }
