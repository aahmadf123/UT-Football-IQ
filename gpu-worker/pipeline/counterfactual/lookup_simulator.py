"""MVP counterfactual coverage simulator — lookup + empirical-Bayes regression (#141).

Given a route run against some coverage, estimate what the *expected-yards
distribution* would have been against a different (counterfactual) coverage,
from historical ``(route_concept × coverage_type) → yards`` observations.

Design constraints (Issue #141 + readiness/constraint follow-ups):

* **Honest, never mock.** A ``(route, coverage)`` cell with no data is not
  fabricated — it is shrunk toward the route prior (same route, other coverages)
  and clearly flagged ``sparse`` / ``prior``, or reported ``insufficient`` when a
  route has never been seen. The simulator never invents an expected value, and
  never borrows a number from unrelated routes.
* **Uncertainty is first-class.** Every distribution carries a sample size, a
  standard-deviation, a 90% spread (p10/p50/p90), a confidence in ``[0, cap]``,
  and a confidence band on the mean. Confidence is deliberately capped below 1.0
  because the whole surface is experimental until Toledo-validated.
* **Concept-level, identity-safe.** Observations are keyed by route / coverage
  concepts only — never named players. Low-confidence identity / tracking /
  calibration is handled by the *caller* (it must keep coach-facing language
  concept-level); this engine emits no player claims to begin with.
* **Provenance-tagged.** Each lookup records the ``source_label`` of the data it
  was built from (``"toledo_film"`` vs the BDB ``offline-pretraining-evaluation
  -only`` marker), so a consumer can never silently confuse Toledo film with NFL
  Big Data Bowl (#164) offline data.

Pure stdlib (``math``/``statistics``) — no numpy/torch, so the same engine logic
can be mirrored in the backend (``app.analytics.counterfactual``). Not routed
through ``pipeline.model_router``: it loads no weights and runs identical
deterministic math in every priority bucket.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

# ── Tuning constants ─────────────────────────────────────────────────────────

# Empirical-Bayes shrinkage strength: a cell's mean is blended with the prior as
# (n * cell + PSEUDO_COUNT * prior) / (n + PSEUDO_COUNT). With few samples the
# prior dominates; with many the cell does.
PSEUDO_COUNT = 5.0

# At/above this many samples a cell is treated as "stable" (well-sampled).
STABLE_SAMPLE = 12

# Below this many samples we report empirical percentiles from a normal-approx
# band (mean ± z·std) instead of order statistics, which are noisy when sparse.
MIN_SAMPLES_FOR_EMPIRICAL_PERCENTILES = 5

# Confidence is capped well below 1.0: this surface is experimental until it is
# Toledo-validated, so it must never *look* authoritative.
CONFIDENCE_CAP = 0.6

# z-scores for the 90% inner spread and the 95% confidence band on the mean.
_Z90 = 1.2816
_Z95 = 1.96

DEFAULT_TOP_N = 3

# Provenance markers — mirror datasets.bdb.schema.USAGE_MARKER so a BDB-derived
# lookup is never confused with Toledo film.
SOURCE_TOLEDO = "toledo_film"
SOURCE_OFFLINE_BDB = "offline-pretraining-evaluation-only"
SOURCE_SYNTHETIC = "synthetic"

# Data-sufficiency tiers (all are still experimental).
SUFF_SUFFICIENT = "sufficient"
SUFF_SPARSE = "sparse"
SUFF_INSUFFICIENT = "insufficient"


# ── Normalization ────────────────────────────────────────────────────────────

# Conservative coverage-string aliasing onto the coverage-GNN taxonomy
# (docs/coverage-pressure-features.md §5). Unknown strings pass through
# normalized rather than being dropped, so the engine works on whatever the
# label corpus actually holds.
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
    return "_".join(
        str(value).strip().lower().replace("-", " ").replace("_", " ").split()
    )


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
    """One historical play: ``route`` run vs ``coverage`` gained ``yards``."""

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
            route_concept=route,
            coverage_type=coverage,
            yards=yards,
            clip_id=self.clip_id,
        )


@dataclass(slots=True)
class YardsDistribution:
    """Expected-yards distribution for a ``(route, coverage)`` cell."""

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
    basis: str  # "empirical" | "shrunk" | "prior" | "insufficient"
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
            "confidence_band": [
                round(self.confidence_low, 2),
                round(self.confidence_high, 2),
            ],
            "basis": self.basis,
            "source": self.source_label,
            "experimental": self.experimental,
        }


@dataclass(slots=True)
class CounterfactualOutcome:
    """A ranked counterfactual coverage and its expected-yards distribution."""

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
    """Full simulator response for one ``(route, factual_coverage)`` query."""

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


# ── Cell accumulator ─────────────────────────────────────────────────────────


@dataclass
class _Cell:
    """Running stats for one bucket of yards observations."""

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
        """Linear-interpolation percentile (``q`` in ``[0, 1]``)."""
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
        """Add one observation. Returns False if it could not be normalized."""
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

    # ── Distribution estimation ───────────────────────────────────────────────

    def expected_yards(self, route: str, coverage: str) -> YardsDistribution:
        """Estimate the expected-yards distribution for one cell.

        Sparse cells are shrunk toward the route prior.
        A cell/route with no data at all is reported ``insufficient`` rather than
        invented.
        """
        route_n = normalize_concept(route) or ""
        cov_n = normalize_coverage(coverage) or ""
        cell = self._cells.get((route_n, cov_n))
        route_prior = self._route_prior.get(route_n)

        # Shrink only toward the *route* prior (same route, other coverages) —
        # the natural empirical-Bayes structure for "what would THIS route do".
        # The global average across unrelated routes is deliberately NOT a
        # fallback: a route we have never seen is honestly ``insufficient``, not
        # a number borrowed from different concepts.
        prior = (
            route_prior if (route_prior is not None and route_prior.n > 0) else _Cell()
        )
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

        # Empirical-Bayes blend toward the prior.
        denom = n + PSEUDO_COUNT
        mean = (n * cell_mean + PSEUDO_COUNT * prior_mean) / denom
        # Blend the spread the same way, and never let it collapse to 0 when we
        # are leaning on the prior (honest about uncertainty).
        std = (n * cell_std + PSEUDO_COUNT * prior_std) / denom
        if std <= 0.0:
            std = prior_std if prior_std > 0 else _fallback_std(prior)

        if n >= MIN_SAMPLES_FOR_EMPIRICAL_PERCENTILES and cell is not None:
            p10, p50, p90 = (
                cell.percentile(0.10),
                cell.percentile(0.50),
                cell.percentile(0.90),
            )
            basis = "empirical" if n >= STABLE_SAMPLE else "shrunk"
        else:
            # Normal-approx spread around the shrunk mean.
            p10, p50, p90 = mean - _Z90 * std, mean, mean + _Z90 * std
            basis = "shrunk" if n > 0 else "prior"

        # Reliability grows with samples, hard-capped because experimental.
        reliability = n / (n + PSEUDO_COUNT)
        confidence = round(min(CONFIDENCE_CAP, CONFIDENCE_CAP * reliability + 0.05), 3)
        if n == 0:
            confidence = round(min(CONFIDENCE_CAP, 0.05), 3)

        # 95% confidence band on the mean, using the effective sample size so a
        # prior-leaning estimate honestly reports a wide band.
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

    # ── Simulation ────────────────────────────────────────────────────────────

    def simulate(
        self,
        route: str,
        *,
        factual_coverage: str | None = None,
        candidate_coverages: list[str] | None = None,
        factual_yards: float | None = None,
        top_n: int = DEFAULT_TOP_N,
    ) -> CounterfactualResult:
        """Rank counterfactual coverages by expected yards for ``route``.

        ``candidate_coverages`` defaults to every coverage seen for the route in
        the corpus. Outcomes are sorted by expected yards (best for the offense
        first) and the top ``top_n`` are returned.
        """
        route_n = normalize_concept(route) or ""
        route_n_samples = self.route_sample_size(route_n)

        factual_dist: YardsDistribution | None = None
        if factual_coverage is not None:
            factual_dist = self.expected_yards(route_n, factual_coverage)

        if candidate_coverages:
            candidates = [normalize_coverage(c) or "" for c in candidate_coverages]
        else:
            candidates = self.coverages_for_route(route_n)
        # Drop the factual coverage and dedupe, preserving a stable order.
        factual_norm = (
            normalize_coverage(factual_coverage) if factual_coverage else None
        )
        seen: set[str] = set()
        ordered_candidates: list[str] = []
        for cov in candidates:
            if not cov or cov == factual_norm or cov in seen:
                continue
            seen.add(cov)
            ordered_candidates.append(cov)

        dists = [self.expected_yards(route_n, cov) for cov in ordered_candidates]
        # Keep only cells we can actually say something about.
        dists = [d for d in dists if d.basis != "insufficient"]
        dists.sort(key=lambda d: d.mean, reverse=True)

        factual_mean = (
            factual_dist.mean
            if factual_dist and factual_dist.basis != "insufficient"
            else None
        )
        outcomes = [
            CounterfactualOutcome(
                rank=i + 1,
                distribution=d,
                delta_vs_factual=(d.mean - factual_mean)
                if factual_mean is not None
                else None,
            )
            for i, d in enumerate(dists[: max(top_n, 0)])
        ]

        sufficiency, note = self._classify(route_n, route_n_samples, outcomes)
        return CounterfactualResult(
            route_concept=route_n,
            factual_coverage=factual_norm,
            factual=factual_dist,
            factual_observed_yards=(
                float(factual_yards) if factual_yards is not None else None
            ),
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
    """A non-zero spread when even the prior is too thin to estimate one."""
    if prior.n >= 2 and prior.std > 0:
        return prior.std
    # 1/3 of the magnitude of the prior mean, floored, so the band is never a
    # misleading zero-width point estimate.
    return max(abs(prior.mean) / 3.0, 1.0)
