"""Counterfactual coverage simulator — backend engine (Issue #141).

Backend-side port of the offline MVP engine that lives in the GPU worker
(``gpu-worker/pipeline/counterfactual/lookup_simulator.py``). The two services do
not share a package, so — exactly like ``app.analytics.tendency_break`` mirrors
``pipeline.play_prediction.tendency_break_engine`` — this is a parallel,
pure-stdlib implementation of the same contract.

What it does: from historical ``(route_concept × coverage_type) → yards``
observations, estimate the expected-yards distribution had a route been run
against a different (counterfactual) coverage. Every estimate is **experimental**,
carries an uncertainty / confidence band, is keyed by route/coverage **concepts**
(never named players), and is reported ``insufficient`` rather than fabricated for
a route we have never seen.

The extraction helpers at the bottom turn the labels/metrics the backend already
holds into observations, and they intentionally read **real measured** outcomes
only (an explicit ``yards_gained`` / ``play_outcome`` label, else the measured
``pre_contact_yards + post_contact_yards``). When no outcome signal exists the
clip simply contributes no observation — the simulator never invents a yardage.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

# ── Tuning constants (kept in sync with the GPU-worker engine) ───────────────

PSEUDO_COUNT = 5.0
STABLE_SAMPLE = 12
MIN_SAMPLES_FOR_EMPIRICAL_PERCENTILES = 5
CONFIDENCE_CAP = 0.6
_Z90 = 1.2816
_Z95 = 1.96
DEFAULT_TOP_N = 3

SOURCE_TOLEDO = "toledo_film"
SOURCE_OFFLINE_BDB = "offline-pretraining-evaluation-only"

SUFF_SUFFICIENT = "sufficient"
SUFF_SPARSE = "sparse"
SUFF_INSUFFICIENT = "insufficient"

# Label/metric vocabulary the extractors read.
_ROUTE_LABEL_TYPES = ("play_concept", "route_concept")
_COVERAGE_LABEL_TYPES = ("coverage_shell", "defensive_shell", "coverage")
_OUTCOME_LABEL_TYPES = ("yards_gained", "play_outcome", "play_result")
_OUTCOME_METRIC_NAMES = ("pre_contact_yards", "post_contact_yards")


# ── Normalization ────────────────────────────────────────────────────────────

_COVERAGE_ALIASES: dict[str, str] = {
    "cover_0": "cover_0",
    "cover_zero": "cover_0",
    "c0": "cover_0",
    "cover_1": "cover_1",
    "cover_one": "cover_1",
    "man_free": "man_free",
    "c1": "cover_1",
    "cover_2": "cover_2_shell",
    "cover_two": "cover_2_shell",
    "c2": "cover_2_shell",
    "cover_2_shell": "cover_2_shell",
    "cover_2_mof": "cover_2_mof",
    "tampa_2": "cover_2_mof",
    "cover_3": "cover_3",
    "cover_three": "cover_3",
    "c3": "cover_3",
    "cover_4": "cover_4",
    "cover_four": "cover_4",
    "quarters": "cover_4",
    "c4": "cover_4",
    "cover_6": "cover_6",
    "cover_six": "cover_6",
    "quarter_quarter_half": "cover_6",
    "bracket_match": "bracket_match",
    "match": "bracket_match",
}


def _slug(value: str) -> str:
    return "_".join(str(value).strip().lower().replace("-", " ").replace("_", " ").split())


def normalize_coverage(value: str | None) -> str | None:
    """Normalize a coverage label onto the canonical taxonomy (best-effort)."""
    if value is None:
        return None
    slug = _slug(value)
    if not slug:
        return None
    return _COVERAGE_ALIASES.get(slug, slug)


def normalize_concept(value: str | None) -> str | None:
    """Normalize a route / play concept to a stable lookup key."""
    if value is None:
        return None
    slug = _slug(value)
    return slug or None


# ── Data model ───────────────────────────────────────────────────────────────


@dataclass(slots=True)
class PlayObservation:
    route_concept: str
    coverage_type: str
    yards: float
    clip_id: str | None = None

    def normalized(self) -> PlayObservation | None:
        route = normalize_concept(self.route_concept)
        coverage = normalize_coverage(self.coverage_type)
        if route is None or coverage is None:
            return None
        try:
            yards = float(self.yards)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(yards):
            return None
        return PlayObservation(
            route_concept=route, coverage_type=coverage, yards=yards, clip_id=self.clip_id
        )


@dataclass(slots=True)
class YardsDistribution:
    coverage_type: str
    sample_size: int
    mean: float
    std: float
    p10: float
    p50: float
    p90: float
    confidence: float
    confidence_low: float
    confidence_high: float
    basis: str
    source_label: str
    experimental: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "coverage_type": self.coverage_type,
            "expected_yards": round(self.mean, 2),
            "sample_size": self.sample_size,
            "std": round(self.std, 2),
            "p10": round(self.p10, 2),
            "p50": round(self.p50, 2),
            "p90": round(self.p90, 2),
            "confidence": round(self.confidence, 3),
            "confidence_band": [round(self.confidence_low, 2), round(self.confidence_high, 2)],
            "basis": self.basis,
            "source": self.source_label,
            "experimental": self.experimental,
        }


@dataclass(slots=True)
class CounterfactualOutcome:
    rank: int
    distribution: YardsDistribution
    delta_vs_factual: float | None

    def to_dict(self) -> dict[str, Any]:
        out = {"rank": self.rank, **self.distribution.to_dict()}
        if self.delta_vs_factual is not None:
            out["delta_vs_factual"] = round(self.delta_vs_factual, 2)
        return out


@dataclass(slots=True)
class CounterfactualResult:
    route_concept: str
    factual_coverage: str | None
    factual: YardsDistribution | None
    factual_observed_yards: float | None
    outcomes: list[CounterfactualOutcome]
    data_sufficiency: str
    route_sample_size: int
    source_label: str
    experimental: bool = True
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "route_concept": self.route_concept,
            "factual_coverage": self.factual_coverage,
            "factual": self.factual.to_dict() if self.factual else None,
            "factual_observed_yards": (
                round(self.factual_observed_yards, 2)
                if self.factual_observed_yards is not None
                else None
            ),
            "outcomes": [o.to_dict() for o in self.outcomes],
            "data_sufficiency": self.data_sufficiency,
            "route_sample_size": self.route_sample_size,
            "source": self.source_label,
            "experimental": self.experimental,
            "note": self.note,
        }


@dataclass
class _Cell:
    yards: list[float] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.yards)

    @property
    def mean(self) -> float:
        return sum(self.yards) / len(self.yards) if self.yards else 0.0

    @property
    def std(self) -> float:
        n = len(self.yards)
        if n < 2:
            return 0.0
        m = self.mean
        var = sum((y - m) ** 2 for y in self.yards) / (n - 1)
        return math.sqrt(max(var, 0.0))

    def percentile(self, q: float) -> float:
        if not self.yards:
            return 0.0
        ordered = sorted(self.yards)
        if len(ordered) == 1:
            return ordered[0]
        pos = q * (len(ordered) - 1)
        lo = math.floor(pos)
        hi = math.ceil(pos)
        if lo == hi:
            return ordered[lo]
        frac = pos - lo
        return ordered[lo] * (1 - frac) + ordered[hi] * frac


# ── The lookup ───────────────────────────────────────────────────────────────


class CounterfactualLookup:
    """Builds and queries the ``(route × coverage) → expected yards`` table."""

    def __init__(self, source_label: str = SOURCE_TOLEDO) -> None:
        self.source_label = source_label
        self._cells: dict[tuple[str, str], _Cell] = defaultdict(_Cell)
        self._route_prior: dict[str, _Cell] = defaultdict(_Cell)
        self._global = _Cell()

    @classmethod
    def from_observations(
        cls, observations: list[PlayObservation], *, source_label: str = SOURCE_TOLEDO
    ) -> CounterfactualLookup:
        lookup = cls(source_label=source_label)
        for obs in observations:
            lookup.add(obs)
        return lookup

    def add(self, obs: PlayObservation) -> bool:
        norm = obs.normalized()
        if norm is None:
            return False
        self._cells[(norm.route_concept, norm.coverage_type)].yards.append(norm.yards)
        self._route_prior[norm.route_concept].yards.append(norm.yards)
        self._global.yards.append(norm.yards)
        return True

    @property
    def total_observations(self) -> int:
        return self._global.n

    def coverages_for_route(self, route: str) -> list[str]:
        route = normalize_concept(route) or ""
        return sorted({cov for (r, cov) in self._cells if r == route})

    def route_sample_size(self, route: str) -> int:
        route = normalize_concept(route) or ""
        return self._route_prior[route].n if route in self._route_prior else 0

    def expected_yards(self, route: str, coverage: str) -> YardsDistribution:
        route_n = normalize_concept(route) or ""
        cov_n = normalize_coverage(coverage) or ""
        cell = self._cells.get((route_n, cov_n))
        route_prior = self._route_prior.get(route_n)

        # Shrink only toward the route prior (same route, other coverages). A
        # route never seen is ``insufficient`` — never a global cross-route guess.
        prior = route_prior if (route_prior is not None and route_prior.n > 0) else _Cell()
        prior_has = prior.n > 0
        if (cell is None or cell.n == 0) and not prior_has:
            return YardsDistribution(
                coverage_type=cov_n,
                sample_size=0,
                mean=0.0,
                std=0.0,
                p10=0.0,
                p50=0.0,
                p90=0.0,
                confidence=0.0,
                confidence_low=0.0,
                confidence_high=0.0,
                basis="insufficient",
                source_label=self.source_label,
            )

        n = cell.n if cell else 0
        cell_mean = cell.mean if cell else 0.0
        cell_std = cell.std if cell else 0.0
        prior_mean = prior.mean
        prior_std = prior.std

        denom = n + PSEUDO_COUNT
        mean = (n * cell_mean + PSEUDO_COUNT * prior_mean) / denom
        std = (n * cell_std + PSEUDO_COUNT * prior_std) / denom
        if std <= 0.0:
            std = prior_std if prior_std > 0 else _fallback_std(prior)

        if n >= MIN_SAMPLES_FOR_EMPIRICAL_PERCENTILES and cell is not None:
            p10, p50, p90 = cell.percentile(0.10), cell.percentile(0.50), cell.percentile(0.90)
            basis = "empirical" if n >= STABLE_SAMPLE else "shrunk"
        else:
            p10, p50, p90 = mean - _Z90 * std, mean, mean + _Z90 * std
            basis = "shrunk" if n > 0 else "prior"

        reliability = n / (n + PSEUDO_COUNT)
        confidence = round(min(CONFIDENCE_CAP, CONFIDENCE_CAP * reliability + 0.05), 3)
        if n == 0:
            confidence = round(min(CONFIDENCE_CAP, 0.05), 3)

        n_eff = max(n + PSEUDO_COUNT, 1.0)
        se = std / math.sqrt(n_eff)
        return YardsDistribution(
            coverage_type=cov_n,
            sample_size=n,
            mean=mean,
            std=std,
            p10=p10,
            p50=p50,
            p90=p90,
            confidence=confidence,
            confidence_low=mean - _Z95 * se,
            confidence_high=mean + _Z95 * se,
            basis=basis,
            source_label=self.source_label,
        )

    def simulate(
        self,
        route: str,
        *,
        factual_coverage: str | None = None,
        candidate_coverages: list[str] | None = None,
        factual_yards: float | None = None,
        top_n: int = DEFAULT_TOP_N,
    ) -> CounterfactualResult:
        route_n = normalize_concept(route) or ""
        route_n_samples = self.route_sample_size(route_n)

        factual_dist: YardsDistribution | None = None
        if factual_coverage is not None:
            factual_dist = self.expected_yards(route_n, factual_coverage)

        if candidate_coverages:
            candidates = [normalize_coverage(c) or "" for c in candidate_coverages]
        else:
            candidates = self.coverages_for_route(route_n)
        factual_norm = normalize_coverage(factual_coverage) if factual_coverage else None
        seen: set[str] = set()
        ordered_candidates: list[str] = []
        for cov in candidates:
            if not cov or cov == factual_norm or cov in seen:
                continue
            seen.add(cov)
            ordered_candidates.append(cov)

        dists = [self.expected_yards(route_n, cov) for cov in ordered_candidates]
        dists = [d for d in dists if d.basis != "insufficient"]
        dists.sort(key=lambda d: d.mean, reverse=True)

        factual_mean = (
            factual_dist.mean if factual_dist and factual_dist.basis != "insufficient" else None
        )
        outcomes = [
            CounterfactualOutcome(
                rank=i + 1,
                distribution=d,
                delta_vs_factual=(d.mean - factual_mean) if factual_mean is not None else None,
            )
            for i, d in enumerate(dists[: max(top_n, 0)])
        ]

        sufficiency, note = self._classify(route_n, route_n_samples, outcomes)
        return CounterfactualResult(
            route_concept=route_n,
            factual_coverage=factual_norm,
            factual=factual_dist,
            factual_observed_yards=(float(factual_yards) if factual_yards is not None else None),
            outcomes=outcomes,
            data_sufficiency=sufficiency,
            route_sample_size=route_n_samples,
            source_label=self.source_label,
            note=note,
        )

    def _classify(
        self, route: str, route_samples: int, outcomes: list[CounterfactualOutcome]
    ) -> tuple[str, str]:
        if route_samples == 0 or not outcomes:
            return (
                SUFF_INSUFFICIENT,
                "No historical reps for this route — nothing to simulate. "
                "Experimental: never shown as a coaching recommendation.",
            )
        best_n = max((o.distribution.sample_size for o in outcomes), default=0)
        if best_n < MIN_SAMPLES_FOR_EMPIRICAL_PERCENTILES:
            return (
                SUFF_SPARSE,
                "Sparse sample — estimates lean on the route prior. "
                "Experimental, directional only; not a coaching recommendation.",
            )
        return (
            SUFF_SUFFICIENT,
            "Experimental estimate from historical reps. Directional only until "
            "Toledo-validated; never a standalone coaching recommendation.",
        )


def _fallback_std(prior: _Cell) -> float:
    if prior.n >= 2 and prior.std > 0:
        return prior.std
    return max(abs(prior.mean) / 3.0, 1.0)


# ── DB-agnostic extraction (labels / metrics → observations) ──────────────────


def _label_text(label_value: dict[str, Any] | None, keys: tuple[str, ...]) -> str | None:
    """Pull the first non-empty string from preferred keys of a label_value."""
    if not isinstance(label_value, dict):
        return None
    for key in keys:
        val = label_value.get(key)
        if isinstance(val, str) and val.strip():
            return val
    return None


def extract_route_concept(labels: list[dict[str, Any]]) -> str | None:
    """Route/play concept from labels (``play_concept`` preferred)."""
    for label_type in _ROUTE_LABEL_TYPES:
        for lbl in labels:
            if lbl.get("label_type") != label_type:
                continue
            text = _label_text(
                lbl.get("label_value"),
                ("concept", "route", "family", "generic", "value", "label", "toledo"),
            )
            if text:
                return text
    return None


def extract_coverage_type(labels: list[dict[str, Any]]) -> str | None:
    """Coverage shell from labels (``coverage_shell`` preferred)."""
    for label_type in _COVERAGE_LABEL_TYPES:
        for lbl in labels:
            if lbl.get("label_type") != label_type:
                continue
            text = _label_text(
                lbl.get("label_value"),
                ("coverage", "shell", "generic", "value", "label", "toledo"),
            )
            if text:
                return text
    return None


def _metric_yards(metric_value: dict[str, Any] | None) -> float | None:
    if not isinstance(metric_value, dict):
        return None
    for key in ("yards", "value"):
        val = metric_value.get(key)
        if isinstance(val, int | float) and not isinstance(val, bool):
            f = float(val)
            if math.isfinite(f):
                return f
    return None


def extract_outcome_yards(
    labels: list[dict[str, Any]], metrics: list[dict[str, Any]]
) -> float | None:
    """Resolve a **measured** per-play yardage, or ``None`` (never fabricated).

    Priority: an explicit ``yards_gained`` / ``play_outcome`` label, else the
    measured ball-carrier yardage ``pre_contact_yards + post_contact_yards``.
    """
    for label_type in _OUTCOME_LABEL_TYPES:
        for lbl in labels:
            if lbl.get("label_type") != label_type:
                continue
            lv = lbl.get("label_value")
            if isinstance(lv, dict):
                for key in ("yards", "yards_gained", "value", "net_yards"):
                    val = lv.get(key)
                    if isinstance(val, int | float) and not isinstance(val, bool):
                        f = float(val)
                        if math.isfinite(f):
                            return f

    carrier = 0.0
    found = False
    for metric in metrics:
        if metric.get("metric_name") in _OUTCOME_METRIC_NAMES:
            yards = _metric_yards(metric.get("metric_value"))
            if yards is not None:
                carrier += yards
                found = True
    return carrier if found else None


def build_observations(
    clip_rows: list[dict[str, Any]],
    labels_by_clip: dict[str, list[dict[str, Any]]],
    metrics_by_clip: dict[str, list[dict[str, Any]]],
) -> list[PlayObservation]:
    """Turn cached clips + labels + metrics into simulator observations.

    A clip contributes an observation only when it has a route concept, a
    coverage type, **and** a measured outcome yardage. Clips missing any of the
    three are silently skipped — the simulator is built on real data only.
    """
    observations: list[PlayObservation] = []
    for clip in clip_rows:
        clip_id = str(clip.get("id"))
        labels = labels_by_clip.get(clip_id, [])
        metrics = metrics_by_clip.get(clip_id, [])
        route = extract_route_concept(labels)
        coverage = extract_coverage_type(labels)
        if route is None or coverage is None:
            continue
        yards = extract_outcome_yards(labels, metrics)
        if yards is None:
            continue
        observations.append(
            PlayObservation(
                route_concept=route, coverage_type=coverage, yards=yards, clip_id=clip_id
            )
        )
    return observations
