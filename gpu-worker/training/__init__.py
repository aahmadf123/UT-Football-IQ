"""Training harnesses for Football-IQ models.

Currently hosts the practice<->game cross-regime self-distillation trainer
(Issue #150 §5.14) and its play-call aligner. Heavy deps (torch/ultralytics) are
imported lazily inside the trainer; importing this package only needs
numpy/structlog so it stays CI-safe.
"""

from __future__ import annotations

from training.cross_regime_distill import (
    MIN_MAP_GAIN_PP,
    DistillConfig,
    DistillReport,
    build_checkpoint,
    build_registration_payload,
    distillation_loss,
    improves_map,
    map50_proxy,
    run_nightly,
    run_synthetic,
)
from training.play_call_aligner import (
    MIN_PAIRED_PLAYS,
    USAGE_MARKER,
    ClipRef,
    PairedPlay,
    align_plays,
    build_pairing_manifest,
    filter_validated,
    meets_min_plays,
)

__all__ = [
    "MIN_MAP_GAIN_PP",
    "MIN_PAIRED_PLAYS",
    "USAGE_MARKER",
    "ClipRef",
    "DistillConfig",
    "DistillReport",
    "PairedPlay",
    "align_plays",
    "build_checkpoint",
    "build_pairing_manifest",
    "build_registration_payload",
    "distillation_loss",
    "filter_validated",
    "improves_map",
    "map50_proxy",
    "meets_min_plays",
    "run_nightly",
    "run_synthetic",
]
