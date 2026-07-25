"""Unit tests for the counterfactual coverage simulator (Issue #141).

Covers the MVP lookup + empirical-Bayes engine and the deferred V2 diffusion
scaffold. The whole surface must be experimental, must carry uncertainty bands,
must never fabricate an estimate for a route it has never seen, and must never
emit named-player claims (it is concept-level by construction).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.counterfactual import (  # noqa: E402
    CounterfactualLookup,
    DiffusionCounterfactualSimulator,
    PlayObservation,
    normalize_coverage,
)
from pipeline.counterfactual.lookup_simulator import (  # noqa: E402
    CONFIDENCE_CAP,
    SOURCE_OFFLINE_BDB,
    SOURCE_TOLEDO,
    SUFF_INSUFFICIENT,
    SUFF_SPARSE,
    SUFF_SUFFICIENT,
)


def _obs(route, coverage, yards):
    return PlayObservation(route_concept=route, coverage_type=coverage, yards=yards)


def _corpus():
    """A small synthetic corpus: 'go' beats cover_1, struggles vs cover_2_shell."""
    obs = []
    obs += [_obs("Go", "Cover 1", y) for y in (18, 22, 25, 20, 16, 24, 19, 21)]
    obs += [_obs("Go", "Cover 3", y) for y in (10, 12, 8, 11, 9, 13)]
    obs += [_obs("Go", "Cover 2 Shell", y) for y in (4, 6, 3, 5, 2, 7)]
    obs += [_obs("Go", "Quarters", y) for y in (14, 16, 12, 15, 13)]  # cover_4
    obs += [_obs("Slant", "Cover 1", y) for y in (7, 9, 6, 8)]
    return obs


# ── Normalization ────────────────────────────────────────────────────────────


def test_coverage_normalization_aliases():
    assert normalize_coverage("Cover 3") == "cover_3"
    assert normalize_coverage("cover-3") == "cover_3"
    assert normalize_coverage("Quarters") == "cover_4"
    assert normalize_coverage("Cover 2 Shell") == "cover_2_shell"
    assert normalize_coverage(None) is None
    # Unknown strings pass through normalized rather than being dropped.
    assert normalize_coverage("Buzz Match") == "buzz_match"


# ── Lookup build + estimation ──────────────────────────────────────────────────


def test_top3_ranked_outcomes_for_route_coverage():
    lookup = CounterfactualLookup.from_observations(_corpus())
    res = lookup.simulate("Go", factual_coverage="Cover 2 Shell", top_n=3)
    assert res.experimental is True
    # Three candidate coverages remain after dropping the factual one.
    assert len(res.outcomes) == 3
    # Ranked best-for-offense first: cover_1 (~20) > cover_3 (~10).
    assert res.outcomes[0].distribution.coverage_type == "cover_1"
    assert res.outcomes[0].distribution.mean > res.outcomes[1].distribution.mean
    # Ranks are 1..n contiguous.
    assert [o.rank for o in res.outcomes] == [1, 2, 3]


def test_outcomes_carry_uncertainty_band():
    lookup = CounterfactualLookup.from_observations(_corpus())
    res = lookup.simulate("Go", factual_coverage="Cover 3")
    top = res.outcomes[0].to_dict()
    assert "expected_yards" in top
    assert "confidence_band" in top and len(top["confidence_band"]) == 2
    assert (
        top["confidence_band"][0] <= top["expected_yards"] <= top["confidence_band"][1]
    )
    assert 0.0 <= top["confidence"] <= CONFIDENCE_CAP
    assert top["experimental"] is True
    assert "p10" in top and "p90" in top
    assert top["p10"] <= top["p50"] <= top["p90"]


def test_delta_vs_factual_present_when_factual_given():
    lookup = CounterfactualLookup.from_observations(_corpus())
    res = lookup.simulate("Go", factual_coverage="Cover 2 Shell", factual_yards=5.0)
    assert res.factual is not None
    assert res.factual_observed_yards == 5.0
    # vs the weak cover_2_shell baseline (~4.5), cover_1 should be a big positive delta.
    cover1 = next(o for o in res.outcomes if o.distribution.coverage_type == "cover_1")
    assert cover1.delta_vs_factual is not None
    assert cover1.delta_vs_factual > 0


def test_confidence_capped_below_one_even_when_well_sampled():
    # 60 reps in one cell — still capped, because the surface is experimental.
    lookup = CounterfactualLookup.from_observations(
        [_obs("Go", "Cover 1", 20) for _ in range(60)]
    )
    dist = lookup.expected_yards("Go", "Cover 1")
    assert dist.basis == "empirical"
    assert dist.confidence <= CONFIDENCE_CAP


def test_unseen_route_is_insufficient_not_fabricated():
    lookup = CounterfactualLookup.from_observations(_corpus())
    res = lookup.simulate("Wheel", factual_coverage="Cover 3")
    assert res.data_sufficiency == SUFF_INSUFFICIENT
    assert res.outcomes == []
    assert res.route_sample_size == 0
    # The cell estimate itself is flagged insufficient, not a guessed number.
    dist = lookup.expected_yards("Wheel", "Cover 3")
    assert dist.basis == "insufficient"
    assert dist.sample_size == 0


def test_sparse_cell_shrinks_toward_route_prior():
    # 'Go' has a strong route prior; a single rep vs an otherwise-unseen coverage
    # should be pulled toward that prior rather than trusted at face value.
    obs = _corpus() + [_obs("Go", "Cover 0", 40)]  # one outlier rep
    lookup = CounterfactualLookup.from_observations(obs)
    dist = lookup.expected_yards("Go", "Cover 0")
    assert dist.sample_size == 1
    assert dist.basis in {"shrunk", "prior"}
    # Pulled well below the lone 40-yard outlier toward the ~13y route prior.
    assert dist.mean < 30
    # Sparse cells keep a non-zero, honest spread.
    assert dist.std > 0
    assert dist.confidence_low < dist.mean < dist.confidence_high


def test_data_sufficiency_tiers():
    lookup = CounterfactualLookup.from_observations(_corpus())
    # Well-sampled route → sufficient.
    res = lookup.simulate("Go")
    assert res.data_sufficiency == SUFF_SUFFICIENT
    # Route with only 4 reps total, all in one coverage → no other candidate.
    sparse = CounterfactualLookup.from_observations(
        [_obs("Drag", "Cover 1", 5), _obs("Drag", "Cover 3", 6)]
    )
    res2 = sparse.simulate("Drag", factual_coverage="Cover 1")
    assert res2.data_sufficiency in {SUFF_SPARSE, SUFF_INSUFFICIENT}


def test_serialization_round_trip_keys():
    lookup = CounterfactualLookup.from_observations(_corpus())
    payload = lookup.simulate("Go", factual_coverage="Cover 3").to_dict()
    for key in (
        "route_concept",
        "factual",
        "outcomes",
        "data_sufficiency",
        "experimental",
    ):
        assert key in payload
    assert payload["source"] == SOURCE_TOLEDO


def test_provenance_marker_for_offline_bdb_source():
    lookup = CounterfactualLookup.from_observations(
        _corpus(), source_label=SOURCE_OFFLINE_BDB
    )
    res = lookup.simulate("Go", factual_coverage="Cover 3")
    assert res.source_label == SOURCE_OFFLINE_BDB
    assert res.outcomes[0].distribution.source_label == SOURCE_OFFLINE_BDB


def test_non_numeric_observation_is_dropped():
    lookup = CounterfactualLookup()
    assert lookup.add(PlayObservation("Go", "Cover 1", float("nan"))) is False
    assert lookup.add(PlayObservation("Go", "Cover 1", 20)) is True
    assert lookup.total_observations == 1


# ── Deferred V2 diffusion scaffold ──────────────────────────────────────────────


def test_diffusion_is_deferred_and_never_fabricates():
    sim = DiffusionCounterfactualSimulator()
    res = sim.simulate(pre_snap_state={}, counterfactual_coverage="cover_3")
    assert res.available is False
    assert res.trajectories == []
    assert res.experimental is True
    assert res.reason  # a non-empty reason code
    assert sim.is_ready is False


def test_diffusion_stays_deferred_even_with_checkpoint_and_flag():
    sim = DiffusionCounterfactualSimulator(enabled=True, checkpoint_path="/tmp/fake.pt")
    res = sim.simulate(counterfactual_coverage="cover_1")
    # Flag on + a checkpoint path still does not run an unvalidated generator.
    assert res.available is False
    assert sim.is_ready is False
    assert res.detail["pretrain_source"] == "offline-pretraining-evaluation-only"
