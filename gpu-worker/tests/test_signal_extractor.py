"""Tests for the pre-snap signal extractor (Issue #135)."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.play_prediction.formation_mlp import FormationClassifier
from pipeline.play_prediction.per_opponent_prior import (
    DEFAULT_ALPHA_PASS,
    DEFAULT_ALPHA_RUN,
    DirichletPrior,
    OpponentPriorStore,
    distance_bucket,
    field_zone,
)
from pipeline.play_prediction.personnel import extract_personnel, position_from_jersey
from pipeline.play_prediction.signal_extractor import (
    PreSnapSignals,
    extract_backfield,
    extract_down_distance_zone,
    extract_signals,
    extract_split,
)

# ── Signal 1: personnel ───────────────────────────────────────────────────────


def test_position_from_jersey_ncaa_ranges() -> None:
    assert position_from_jersey(55) == "OL"
    assert position_from_jersey(22) == "RB"
    assert position_from_jersey(15) == "WR"
    assert position_from_jersey(85) == "WR"
    assert position_from_jersey(5) == "QB"
    assert position_from_jersey(None) is None


def test_personnel_prefers_roster_position() -> None:
    players = [
        {"position": "RB", "jersey_number": 55},  # roster wins over OL range
        {"position": "TE", "jersey_number": 88},
        {"position": "QB", "jersey_number": 12},
    ]
    sig = extract_personnel(players)
    assert sig.n_rb == 1
    assert sig.n_te == 1
    assert sig.source == "roster"


def test_personnel_grouping_shorthand_11() -> None:
    players = (
        [{"position": "OL", "jersey_number": 50 + i} for i in range(5)]
        + [{"position": "QB", "jersey_number": 12}]
        + [{"position": "RB", "jersey_number": 22}]
        + [{"position": "TE", "jersey_number": 88}]
        + [{"position": "WR", "jersey_number": 80 + i} for i in range(3)]
    )
    sig = extract_personnel(players)
    assert sig.grouping == "11"
    assert sig.n_wr == 3
    assert sig.confidence == 1.0


def test_personnel_jersey_fallback_lowers_confidence() -> None:
    players = [{"jersey_number": 22}, {"jersey_number": 88}, {}]
    sig = extract_personnel(players)
    assert sig.source == "jersey"
    assert sig.confidence < 1.0


# ── Signal 3: backfield depth ─────────────────────────────────────────────────


def test_backfield_under_center() -> None:
    sig = extract_backfield((49.0, 0.0), los_x=50.0)
    assert sig.value == "under_center"


def test_backfield_shotgun() -> None:
    sig = extract_backfield((44.0, 0.0), los_x=50.0)
    assert sig.value == "shotgun"


def test_backfield_pistol() -> None:
    sig = extract_backfield((46.0, 0.0), los_x=50.0)
    assert sig.value == "pistol"


def test_backfield_missing_qb_is_unknown_low_confidence() -> None:
    sig = extract_backfield(None, los_x=50.0)
    assert sig.value == "unknown"
    assert sig.confidence < 0.2


def test_backfield_qb_in_front_of_los_degrades_to_unknown() -> None:
    sig = extract_backfield((52.0, 0.0), los_x=50.0)
    assert sig.value == "unknown"
    assert sig.confidence < 0.2


# ── Signal 4: receiver split ──────────────────────────────────────────────────


def test_split_wide() -> None:
    # Receiver well off the sideline (|y| small → far from sideline).
    sig = extract_split([(50.0, 5.0)])
    assert sig.value == "wide"


def test_split_tight() -> None:
    # Receiver pinned near the sideline (|y| ~ 26.665).
    sig = extract_split([(50.0, 25.0)])
    assert sig.value == "tight"


def test_split_empty_is_unknown() -> None:
    sig = extract_split([])
    assert sig.value == "unknown"


# ── Signal 6: down-distance + zone ────────────────────────────────────────────


def test_distance_bucket_boundaries() -> None:
    assert distance_bucket(2) == "short"
    assert distance_bucket(3) == "short"
    assert distance_bucket(7) == "medium"
    assert distance_bucket(10) == "long"


def test_field_zone_mapping() -> None:
    assert field_zone(5) == "backed_up"
    assert field_zone(40) == "own_territory"
    assert field_zone(60) == "opp_territory"
    assert field_zone(85) == "red_zone"
    assert field_zone(None) == "unknown"


def test_down_distance_zone_from_event_log_high_confidence() -> None:
    sig = extract_down_distance_zone(3, 9.0, 85.0, from_event_log=True)
    assert sig.value == "3_long_red_zone"
    assert sig.confidence > 0.7


def test_down_distance_inferred_lower_confidence() -> None:
    log_sig = extract_down_distance_zone(1, 10.0, 50.0, from_event_log=True)
    inf_sig = extract_down_distance_zone(1, 10.0, 50.0, from_event_log=False)
    assert inf_sig.confidence < log_sig.confidence


def test_down_distance_missing_is_unknown() -> None:
    sig = extract_down_distance_zone(None, None, 50.0)
    assert sig.value == "unknown"


# ── Orchestration + serialization ─────────────────────────────────────────────


def _full_offense() -> list[dict[str, object]]:
    players: list[dict[str, object]] = [
        {"position": "OL", "jersey_number": 50 + i, "x": 50.0, "y": -2.0 + i}
        for i in range(5)
    ]
    players.append({"position": "QB", "jersey_number": 12, "x": 44.0, "y": 0.0})
    players.append({"position": "RB", "jersey_number": 22, "x": 43.0, "y": 0.0})
    players.append({"position": "TE", "jersey_number": 88, "x": 50.0, "y": 3.0})
    players += [
        {"position": "WR", "jersey_number": 80, "x": 50.0, "y": 20.0},
        {"position": "WR", "jersey_number": 81, "x": 50.0, "y": -18.0},
        {"position": "WR", "jersey_number": 82, "x": 50.0, "y": 12.0},
    ]
    return players


def test_extract_signals_full_play_serializes_to_jsonb() -> None:
    import json

    signals = extract_signals(
        offense_players=_full_offense(),
        los_x=50.0,
        down=3,
        distance_yards=8.0,
        motion_trajectory=[(40.0, -10.0), (45.0, -3.0), (50.0, 2.0)],
    )
    assert isinstance(signals, PreSnapSignals)
    d = signals.to_dict()
    # All six signals present.
    for key in ("personnel", "formation", "backfield", "split", "motion", "down_distance_zone"):
        assert key in d
        assert "confidence" in d[key]
    # JSONB-serializable (round-trips through json).
    assert json.loads(json.dumps(d)) == d
    assert d["schema_version"] == 1


def test_extract_signals_each_signal_exposes_confidence() -> None:
    signals = extract_signals(offense_players=_full_offense(), los_x=50.0, down=1, distance_yards=10.0)
    confs = signals.signal_confidences()
    assert set(confs.keys()) == {
        "personnel",
        "formation",
        "backfield",
        "split",
        "motion",
        "down_distance_zone",
    }
    assert all(0.0 <= c <= 1.0 for c in confs.values())


def test_extract_signals_degrades_gracefully_on_empty_input() -> None:
    # Fallback behavior: no players, no down/distance → low-confidence unknowns,
    # but no exception and a fully-formed signal vector.
    signals = extract_signals(offense_players=[], los_x=50.0)
    d = signals.to_dict()
    assert d["personnel"]["value"] == "00"  # zero RB/TE
    assert d["down_distance_zone"]["value"] == "unknown"
    assert signals.mean_confidence < 0.5


def test_formation_geometric_ignores_players_in_front_of_los_for_backfield_count() -> None:
    positions = [(50.0, -2.0 + i) for i in range(5)]
    positions += [(47.0, 0.0), (46.0, 0.0), (45.0, 0.0), (53.0, 0.0)]
    positions += [(50.0, 12.0), (50.0, -12.0)]
    signal = FormationClassifier().classify(positions, los_x=50.0)
    assert signal.label == "i_form"


def test_formation_geometric_respects_reverse_offense_direction() -> None:
    positions = [(50.0, -2.0 + i) for i in range(5)]
    positions += [(53.0, 0.0), (54.0, 0.0), (55.0, 0.0), (47.0, 0.0)]
    positions += [(50.0, 12.0), (50.0, -12.0)]
    signal = FormationClassifier().classify(positions, los_x=50.0, offense_direction=-1)
    assert signal.label == "i_form"


def test_feature_vector_is_numeric_and_fixed_length() -> None:
    signals = extract_signals(offense_players=_full_offense(), los_x=50.0, down=2, distance_yards=4.0)
    fv = signals.feature_vector()
    assert fv.shape == (8,)


# ── Per-opponent Dirichlet priors ─────────────────────────────────────────────


def test_default_prior_is_uninformative_half() -> None:
    prior = DirichletPrior()
    assert prior.alpha_pass == DEFAULT_ALPHA_PASS
    assert prior.alpha_run == DEFAULT_ALPHA_RUN
    assert prior.pass_probability() == 0.5
    assert not prior.is_data_backed


def test_prior_updates_toward_observations() -> None:
    prior = DirichletPrior()
    for _ in range(20):
        prior.update("pass")
    assert prior.pass_probability() > 0.8
    assert prior.is_data_backed
    assert prior.variance() < DirichletPrior().variance()


def test_prior_rejects_bad_class() -> None:
    import pytest

    with pytest.raises(ValueError):
        DirichletPrior().update("punt")


def test_store_get_creates_safe_default_cell() -> None:
    store = OpponentPriorStore()
    cell = store.get("Ohio", 3, 8.0, 85.0)
    # A brand-new opponent cell must default to 0.5 — never a fabricated tendency.
    assert cell.pass_probability() == 0.5
    assert not cell.is_data_backed


def test_store_update_and_log_odds_feed_ensemble() -> None:
    store = OpponentPriorStore()
    for _ in range(10):
        store.update("Ohio", 3, 8.0, 85.0, "pass")
    cell = store.get("Ohio", 3, 8.0, 85.0)
    assert cell.log_odds() > 0  # pass-leaning
    assert cell.is_data_backed


def test_store_roundtrips_through_rows() -> None:
    store = OpponentPriorStore()
    store.update("Ohio", 1, 10.0, 50.0, "run")
    store.update("Ohio", 1, 10.0, 50.0, "run")
    rows = store.to_rows()
    restored = OpponentPriorStore.from_rows(rows)
    assert restored.get("Ohio", 1, 10.0, 50.0).n_run == 2
