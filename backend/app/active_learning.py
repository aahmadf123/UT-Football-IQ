"""Active-learning prioritisation policy (Issues #145 / #146).

This module is the **single, pure** place where Football-IQ turns a model
output's *calibrated* uncertainty into an active-learning queue priority. It is
deliberately free of FastAPI / SQLAlchemy so it can be unit-tested in isolation
and reused by every producer of ``active_learning_queue`` rows (today the MLOps
nightly sampler in ``app.routers.mlops``; tomorrow any GPU-worker callback).

The uncertainty contract is the one the GPU-worker emits via
``pipeline.calibration.CalibratedOutput`` (#146). The producer payload stored on
a label (``label_value["uncertainty"]``) / a queue row
(``active_learning_queue.metadata["uncertainty"]``) is::

    {
        "calibrated": bool,        # was a fitted calibrator applied?
        "method": str,             # "temperature" | "platt" | "uncalibrated" ...
        "confidence": float|None,  # calibrated probability — ONLY when calibrated
        "entropy": float|None,     # normalised Shannon entropy in [0, 1]
        "raw_score": float|None,   # uncalibrated ranking signal (never shown as confidence)
    }

Two safety rules are baked in, matching the producer side:

* An **uncalibrated** score is never treated as a trustworthy confidence. Its
  magnitude may still drive *ranking* (low score == worth a look) via
  ``raw_score``, but it is never surfaced as a calibrated ``confidence``.
* A **missing** uncertainty signal does not silently drop the item — it still
  earns a defined fallback priority so unscored model output remains reviewable
  rather than being presented as if it were confident.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.models import ActiveLearningReason

# Bump when the stored uncertainty payload shape changes.
UNCERTAINTY_SCHEMA_VERSION = 1

# Canonical keys of the stored / API uncertainty payload.
UNCERTAINTY_KEYS = ("calibrated", "method", "confidence", "entropy", "raw_score")

# Priority "basis" — how the score below was derived. Surfaced to the UI so a
# coach can tell a genuinely-low calibrated confidence apart from an
# uncalibrated or unscored output.
BASIS_CALIBRATED = "calibrated"
BASIS_UNCALIBRATED = "uncalibrated"
BASIS_MISSING = "missing"

# Defaults; the caller overrides these from ``app.config.Settings`` so they are
# tunable per-environment without code changes.
DEFAULT_ENTROPY_WEIGHT = 0.5
DEFAULT_UNCALIBRATED_PRIORITY = 0.6
DEFAULT_MISSING_PRIORITY = 0.5

# ── Corrections queue constants (used by app.routers.corrections) ─────────────

# Default number of clips returned by the annotation queue.
DEFAULT_QUEUE_LIMIT = 25
MAX_QUEUE_LIMIT = 200

# Priority assigned to a clip with no uncertainty score. Negative so it always
# sorts below any real score in the closed unit interval [0, 1].
UNSCORED_PRIORITY = -1.0

# Per-item reason codes (mirror the spirit of ``ActiveLearningReason`` without
# coupling the read-only queue to that write-side enum).
REASON_UNSCORED = "unscored"
REASON_CALIBRATED = "uncertainty_calibrated"
REASON_UNCALIBRATED = "uncertainty_uncalibrated"


@dataclass(frozen=True)
class UncertaintySignal:
    """A parsed, normalised calibrated-uncertainty signal.

    ``confidence`` is populated **only** when ``calibrated`` is ``True`` — an
    uncalibrated raw score is held separately in ``raw_score`` (ranking only) so
    downstream code cannot accidentally present it as a probability.
    """

    calibrated: bool
    method: str
    confidence: float | None
    entropy: float | None
    raw_score: float | None = None

    def as_payload(self) -> dict[str, Any]:
        """Canonical dict for persistence in ``active_learning_queue.metadata``."""
        return {
            "calibrated": self.calibrated,
            "method": self.method,
            "confidence": self.confidence,
            "entropy": self.entropy,
            "raw_score": self.raw_score,
        }


@dataclass(frozen=True)
class PriorityDecision:
    """The output of the prioritisation policy."""

    priority: float
    reason: ActiveLearningReason
    basis: str


def _coerce_unit_float(value: Any) -> float | None:
    """Coerce ``value`` to a float clamped to ``[0, 1]``; ``None`` if not numeric."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return _clamp(float(value))


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def parse_uncertainty(payload: Any) -> UncertaintySignal | None:
    """Parse a raw uncertainty payload into an :class:`UncertaintySignal`.

    Returns ``None`` when the payload is absent or carries no usable signal. The
    parse is deliberately defensive: a malformed payload must never raise inside
    a request handler.
    """
    if not isinstance(payload, dict) or not payload:
        return None

    # Accept both this module's canonical keys and the producer-side
    # ``CalibratedOutput.to_dict()`` aliases (#146): ``is_calibrated``,
    # ``calibration_method`` and ``uncertainty`` (the entropy field).
    calibrated = bool(payload.get("calibrated", payload.get("is_calibrated", False)))
    method = str(payload.get("method") or payload.get("calibration_method") or "none")
    entropy_value = payload.get("entropy")
    if entropy_value is None:
        entropy_value = payload.get("uncertainty")
    entropy = _coerce_unit_float(entropy_value)
    confidence_field = _coerce_unit_float(payload.get("confidence"))
    explicit_raw = _coerce_unit_float(payload.get("raw_score"))

    if calibrated:
        # The load-bearing guard against "uncalibrated scores presented as
        # confidence" (#146): only a calibrated payload yields a confidence.
        confidence = confidence_field
        raw_score = explicit_raw
    else:
        confidence = None
        # A raw/uncalibrated confidence is demoted to a ranking-only signal.
        raw_score = explicit_raw if explicit_raw is not None else confidence_field

    if not calibrated and confidence is None and entropy is None and raw_score is None:
        return None

    return UncertaintySignal(
        calibrated=calibrated,
        method=method,
        confidence=confidence,
        entropy=entropy,
        raw_score=raw_score,
    )


def signal_from_label_value(label_value: Any) -> UncertaintySignal | None:
    """Derive an uncertainty signal from a ``Label.label_value`` payload.

    Prefers the structured ``label_value["uncertainty"]`` payload (the producer
    contract, #146). Falls back to a legacy bare ``label_value["confidence"]``,
    which is treated as **uncalibrated** — recorded for ranking but never
    surfaced as a calibrated confidence.
    """
    if not isinstance(label_value, dict):
        return None

    parsed = parse_uncertainty(label_value.get("uncertainty"))
    if parsed is not None:
        return parsed

    legacy = _coerce_unit_float(label_value.get("confidence"))
    if legacy is None:
        return None
    return UncertaintySignal(
        calibrated=False,
        method="none",
        confidence=None,
        entropy=None,
        raw_score=legacy,
    )


def annotation_priority(
    signal: UncertaintySignal | None,
    *,
    entropy_weight: float = DEFAULT_ENTROPY_WEIGHT,
    uncalibrated_priority: float = DEFAULT_UNCALIBRATED_PRIORITY,
    missing_priority: float = DEFAULT_MISSING_PRIORITY,
) -> PriorityDecision:
    """Map an uncertainty signal to a queue priority in ``[0, 1]``.

    * **Calibrated** signal: blend high entropy and low calibrated confidence —
      the higher either is, the more a coach review is worth. Reason
      ``low_confidence``.
    * **Uncalibrated** signal: there is no trustworthy confidence, but a low raw
      score or high entropy still flags it. Reason ``low_confidence`` when a raw
      magnitude drove it, else ``uncertainty_sampling``.
    * **Missing** signal: defined fallback priority so unscored output is still
      reviewable, never presented as confident. Reason ``uncertainty_sampling``.
    """
    weight = _clamp(entropy_weight)

    if signal is None:
        return PriorityDecision(
            priority=_clamp(missing_priority),
            reason=ActiveLearningReason.uncertainty_sampling,
            basis=BASIS_MISSING,
        )

    if signal.calibrated:
        entropy = signal.entropy if signal.entropy is not None else 0.0
        confidence = signal.confidence if signal.confidence is not None else 0.0
        score = weight * entropy + (1.0 - weight) * (1.0 - confidence)
        return PriorityDecision(
            priority=_clamp(score),
            reason=ActiveLearningReason.low_confidence,
            basis=BASIS_CALIBRATED,
        )

    # Uncalibrated: rank on whatever calibration-independent signal we have.
    candidates: list[float] = []
    if signal.raw_score is not None:
        candidates.append(1.0 - signal.raw_score)
    if signal.entropy is not None:
        candidates.append(signal.entropy)

    if not candidates:
        return PriorityDecision(
            priority=_clamp(uncalibrated_priority),
            reason=ActiveLearningReason.uncertainty_sampling,
            basis=BASIS_UNCALIBRATED,
        )

    reason = (
        ActiveLearningReason.low_confidence
        if signal.raw_score is not None
        else ActiveLearningReason.uncertainty_sampling
    )
    return PriorityDecision(
        priority=_clamp(max(candidates)),
        reason=reason,
        basis=BASIS_UNCALIBRATED,
    )


def basis_for(signal: UncertaintySignal | None) -> str:
    """Display basis for a (possibly missing) signal."""
    if signal is None:
        return BASIS_MISSING
    return BASIS_CALIBRATED if signal.calibrated else BASIS_UNCALIBRATED


# ── Corrections-queue helpers (used by app.routers.corrections) ───────────────


def effective_priority(
    uncertainty_score: float | None,
    uncertainty_calibrated: bool = False,
) -> float:
    """Return the ranking value for one clip (higher = review first).

    A real score is clamped to ``[0, 1]``. A missing score returns
    :data:`UNSCORED_PRIORITY` so it sorts below every scored clip — an unknown
    uncertainty is never inflated into a confident-looking number (#146).

    Calibration does **not** change the ranking value: entropy is the
    active-learning signal regardless of calibration. The
    ``uncertainty_calibrated`` flag governs *honest presentation*, not order,
    and is surfaced separately on each queue item.
    """
    if uncertainty_score is None:
        return UNSCORED_PRIORITY
    return _clamp(float(uncertainty_score))


def queue_reason(uncertainty_score: float | None, uncertainty_calibrated: bool) -> str:
    """Classify why a clip is in the queue (drives the UI badge)."""
    if uncertainty_score is None:
        return REASON_UNSCORED
    return REASON_CALIBRATED if uncertainty_calibrated else REASON_UNCALIBRATED


def normalize_limit(limit: int | None) -> int:
    """Clamp a requested queue ``limit`` into ``[1, MAX_QUEUE_LIMIT]``."""
    if limit is None:
        return DEFAULT_QUEUE_LIMIT
    return max(1, min(int(limit), MAX_QUEUE_LIMIT))
