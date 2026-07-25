"""Clip-level active-learning uncertainty aggregation (Issues #145 / #146).

The annotation queue (#145) prioritizes coach review by the entropy of the
Phase-2 ensemble: ``score(x) = -Σ P(y=c|x) log P(y=c|x)``, evaluated across the
per-play classifier heads (formation, coverage, run/pass, …). Each head already
returns a :class:`~pipeline.calibration.calibrated_output.CalibratedOutput`
carrying ``entropy`` (normalized Shannon entropy in ``[0, 1]``) and an
``is_calibrated`` flag.

This module reduces those per-head outputs to a single clip-level signal the GPU
worker writes back to ``clips.uncertainty_score`` / ``clips.uncertainty_calibrated``
(via ``PATCH /api/v1/clips/{id}``). The backend then orders the queue by it.

Aggregation rule: the clip-level uncertainty is the **maximum** head entropy —
the head the model is *least* sure about drives review (the standard
uncertainty-sampling choice; averaging would let confident heads mask a single
very-uncertain one). The clip is reported ``calibrated`` only when **every**
contributing head is calibrated, so a single uncalibrated head never lets a raw
softmax be presented as a trusted confidence (#146).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from pipeline.calibration.calibrated_output import CalibratedOutput


@dataclass(slots=True)
class ClipUncertainty:
    """Reduced clip-level uncertainty signal for the annotation queue."""

    # None when no head produced an output (nothing to prioritize on yet).
    score: float | None
    is_calibrated: bool
    # Names of the heads that contributed, for the audit/debug payload.
    contributing: tuple[str, ...]

    def to_payload(self) -> dict[str, Any]:
        """Body fragment for ``PATCH /api/v1/clips/{id}`` (#145 columns)."""
        return {
            "uncertainty_score": self.score,
            "uncertainty_calibrated": self.is_calibrated,
        }


def _iter_named(
    outputs: Mapping[str, CalibratedOutput] | Iterable[CalibratedOutput],
) -> list[tuple[str, CalibratedOutput]]:
    if isinstance(outputs, Mapping):
        return list(outputs.items())
    named: list[tuple[str, CalibratedOutput]] = []
    for i, out in enumerate(outputs):
        name = out.value if isinstance(out.value, str) else f"head_{i}"
        named.append((name, out))
    return named


def clip_uncertainty(
    outputs: Mapping[str, CalibratedOutput] | Iterable[CalibratedOutput],
) -> ClipUncertainty:
    """Reduce per-head :class:`CalibratedOutput`\\ s to one clip-level signal.

    Returns the **max** head entropy as the priority score and marks the clip
    calibrated only if every contributing head is calibrated. An empty input
    yields ``score=None`` (the clip stays "unscored" — never a fabricated value).
    """
    named = _iter_named(outputs)
    if not named:
        return ClipUncertainty(score=None, is_calibrated=False, contributing=())

    top_name, top = max(named, key=lambda kv: float(kv[1].entropy))
    score = float(top.entropy)
    score = 0.0 if score < 0.0 else 1.0 if score > 1.0 else score
    all_calibrated = all(o.is_calibrated for _, o in named)
    return ClipUncertainty(
        score=score,
        is_calibrated=all_calibrated,
        contributing=tuple(name for name, _ in named),
    )
