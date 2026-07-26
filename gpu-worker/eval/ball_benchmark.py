"""Offline ball-detector benchmark.

Answers one question per candidate model: on *our* footage, at what confidence
does it find the ball often enough to be worth wiring in, and how much junk does
it emit on frames where there is no ball?

Deliberately not wired into the pipeline. Nothing here changes production
behaviour; ``BallDetectorAdapter``, the SAHI regime routing and the canonical
``{bbox, confidence, class: "ball"}`` output are untouched. This only measures.

Design notes that matter for the result being honest:

**Split by video, never by frame.** Adjacent frames of one play are near
duplicates; a random frame split puts the same ball, half a second apart, in
both train and test and reports a number that has nothing to do with new
footage.

**Report per regime, never blended.** Drone-follow and fixed-sideline are
different detection problems -- 15px ball under SAHI tiling versus a larger one
full-frame. A single combined F1 hides one regime failing behind the other
succeeding.

**A frame with no confidently locatable ball is a hard negative, not a gap.**
``visible=false`` frames carry no box and are scored only for false positives.
Synthesising a box for an occluded ball would train and measure a fiction.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

#: Confidence thresholds swept for every model and regime.
CONFIDENCE_SWEEP: tuple[float, ...] = (0.05, 0.10, 0.15, 0.20, 0.25, 0.30)

#: Regime-appropriate inference modes, matching the production routing.
DRONE_TILE_PX = 128
DRONE_TILE_OVERLAP = 0.2

#: A prediction counts as a hit when its centre is within this many pixels of
#: the labelled centre. Generous relative to a ~15px ball: we are asking "did it
#: find the ball", not "how tight is the box".
CENTRE_TOLERANCE_PX = 20.0

#: Production gate for drone footage. A model clearing all three is worth
#: wiring in; one that does not is not, and no threshold choice rescues it.
GATE_MIN_RECALL = 0.70
GATE_MIN_PRECISION = 0.80
GATE_MAX_FP_PER_ABSENT_FRAME = 0.10


@dataclass(frozen=True)
class LabelledFrame:
    """One evaluation frame and its human label."""

    video: str
    frame: int
    regime: str
    #: True only when a human could confidently locate the ball and draw a box.
    visible: bool
    #: Ball centre in pixels; None whenever ``visible`` is False.
    centre: tuple[float, float] | None = None
    #: Free-text note for failure analysis, e.g. "occluded by centre".
    note: str = ""

    def __post_init__(self) -> None:
        if self.visible and self.centre is None:
            raise ValueError(f"{self.video}#{self.frame}: visible with no centre")
        if not self.visible and self.centre is not None:
            raise ValueError(
                f"{self.video}#{self.frame}: not visible but carries a centre -- "
                "a synthetic box for an occluded ball measures a fiction"
            )


@dataclass
class Prediction:
    """One model output, in image pixels."""

    x: float
    y: float
    confidence: float
    width: float = 0.0
    height: float = 0.0


@dataclass
class RegimeMetrics:
    """Scores for one (model, regime, threshold). Never blended across regimes."""

    model: str
    regime: str
    confidence: float
    visible_frames: int
    absent_frames: int
    true_positives: int
    false_negatives: int
    false_positives: int
    centre_error_px: float | None

    @property
    def recall(self) -> float:
        """Visible-ball recall. Undefined with no visible balls to find."""
        return self.true_positives / self.visible_frames if self.visible_frames else float("nan")

    @property
    def precision(self) -> float:
        predicted = self.true_positives + self.false_positives
        return self.true_positives / predicted if predicted else float("nan")

    @property
    def f1(self) -> float:
        r, p = self.recall, self.precision
        if math.isnan(r) or math.isnan(p) or (r + p) == 0:
            return float("nan")
        return 2 * r * p / (r + p)

    @property
    def fp_per_absent_frame(self) -> float:
        return self.false_positives / self.absent_frames if self.absent_frames else float("nan")

    def clears_gate(self) -> bool:
        """Does this configuration meet the production bar for drone footage?

        ``nan`` fails. An undefined recall means there was nothing to find, which
        is a reason to distrust the whole measurement rather than to pass it.
        """
        return (
            not math.isnan(self.recall)
            and not math.isnan(self.precision)
            and self.recall >= GATE_MIN_RECALL
            and self.precision >= GATE_MIN_PRECISION
            and self.fp_per_absent_frame <= GATE_MAX_FP_PER_ABSENT_FRAME
        )

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out.update(
            recall=_j(self.recall),
            precision=_j(self.precision),
            f1=_j(self.f1),
            fp_per_absent_frame=_j(self.fp_per_absent_frame),
            clears_gate=self.clears_gate(),
        )
        return out


def _j(value: float) -> float | None:
    """JSON has no NaN. ``None`` reads as "undefined", which is what it means."""
    return None if math.isnan(value) else round(value, 4)


def tile_origins(
    width: int, height: int, tile: int = DRONE_TILE_PX, overlap: float = DRONE_TILE_OVERLAP
) -> list[tuple[int, int]]:
    """Top-left corners for SAHI tiling, matching the production ball routing.

    The final row and column are pinned flush to the far edge rather than
    striding past it, so a ball in the last few pixels is covered by a whole
    tile instead of a sliver.
    """
    step = max(1, int(tile * (1.0 - overlap)))

    def axis(extent: int) -> list[int]:
        if extent <= tile:
            # One tile already covers it; striding would emit the same origin
            # twice and double-count every detection on a small frame.
            return [0]
        origins = list(range(0, extent - tile + 1, step))
        if origins[-1] != extent - tile:
            origins.append(extent - tile)
        return origins

    return [(x, y) for y in axis(height) for x in axis(width)]


def score_frame(
    label: LabelledFrame,
    predictions: Sequence[Prediction],
    threshold: float,
    tolerance: float = CENTRE_TOLERANCE_PX,
) -> tuple[int, int, int, float | None]:
    """Score one frame, returning ``(tp, fn, fp, centre_error)``.

    At most one true positive per frame -- there is only ever one ball, so a
    model that sprays boxes cannot earn extra credit for the one that lands.
    Every other surviving prediction is a false positive, which is what makes a
    model like ``instant-1`` (190 detections on a single crop) score as badly as
    it deserves rather than being rewarded for covering the frame.
    """
    kept = [p for p in predictions if p.confidence >= threshold]

    if not label.visible:
        return 0, 0, len(kept), None

    assert label.centre is not None  # guaranteed by __post_init__
    cx, cy = label.centre
    best: Prediction | None = None
    best_dist = float("inf")
    for p in kept:
        d = math.hypot(p.x - cx, p.y - cy)
        if d <= tolerance and d < best_dist:
            best, best_dist = p, d

    if best is None:
        return 0, 1, len(kept), None
    return 1, 0, len(kept) - 1, best_dist


def evaluate(
    labels: Iterable[LabelledFrame],
    predict: Callable[[LabelledFrame], Sequence[Prediction]],
    model: str,
    thresholds: Sequence[float] = CONFIDENCE_SWEEP,
) -> list[RegimeMetrics]:
    """Score a model across every regime present and every threshold.

    ``predict`` is injected so the same scoring runs against a hosted API, a
    local checkpoint, or a stub in tests -- the benchmark should not care where
    the boxes came from, only what they were.
    """
    frames = list(labels)
    cached = {(f.video, f.frame): list(predict(f)) for f in frames}

    results: list[RegimeMetrics] = []
    for regime in sorted({f.regime for f in frames}):
        subset = [f for f in frames if f.regime == regime]
        for threshold in thresholds:
            tp = fn = fp = 0
            errors: list[float] = []
            for f in subset:
                a, b, c, err = score_frame(f, cached[(f.video, f.frame)], threshold)
                tp, fn, fp = tp + a, fn + b, fp + c
                if err is not None:
                    errors.append(err)
            results.append(
                RegimeMetrics(
                    model=model,
                    regime=regime,
                    confidence=threshold,
                    visible_frames=sum(1 for f in subset if f.visible),
                    absent_frames=sum(1 for f in subset if not f.visible),
                    true_positives=tp,
                    false_negatives=fn,
                    false_positives=fp,
                    centre_error_px=round(sum(errors) / len(errors), 2) if errors else None,
                )
            )
    return results


def split_by_video(labels: Iterable[LabelledFrame], holdout: set[str]) -> tuple[list, list]:
    """Partition by source video, never by frame.

    Adjacent frames of one play are near duplicates. A random split puts the
    same ball, half a second apart, on both sides and reports a number that says
    nothing about footage the model has not seen.
    """
    frames = list(labels)
    test = [f for f in frames if f.video in holdout]
    train = [f for f in frames if f.video not in holdout]
    return train, test


@dataclass
class BenchmarkRun:
    """One versioned benchmark, stored so later retraining compares honestly."""

    version: str
    footage: str
    notes: str = ""
    metrics: list[RegimeMetrics] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(
            {
                "version": self.version,
                "footage": self.footage,
                "notes": self.notes,
                "gate": {
                    "min_recall": GATE_MIN_RECALL,
                    "min_precision": GATE_MIN_PRECISION,
                    "max_fp_per_absent_frame": GATE_MAX_FP_PER_ABSENT_FRAME,
                },
                "results": [m.as_dict() for m in self.metrics],
            },
            indent=2,
        )

    def write(self, root: Path) -> Path:
        """Store by version, so a later model is compared and not just replaced."""
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"ball-benchmark-{self.version}.json"
        path.write_text(self.to_json())
        return path

    def best_per_regime(self) -> dict[str, RegimeMetrics | None]:
        """Highest-F1 configuration per regime, or None where nothing scored."""
        out: dict[str, RegimeMetrics | None] = {}
        for regime in sorted({m.regime for m in self.metrics}):
            scored = [m for m in self.metrics if m.regime == regime and not math.isnan(m.f1)]
            out[regime] = max(scored, key=lambda m: m.f1) if scored else None
        return out
