"""Counterfactual coverage simulator (Issue #141).

Offline / backend MVP only. Given a play that gained *Y* yards on ``route`` vs a
``coverage``, estimate the expected-yards distribution had the same route been
run against a different coverage. The MVP is a deterministic lookup + empirical
Bayes regression over historical ``(route_concept × coverage_type) → yards``
observations (:mod:`pipeline.counterfactual.lookup_simulator`).

Per the Issue #141 readiness review and follow-up constraints:

* This is **offline / backend-only**. There is **no** coach-facing "What-if"
  frontend surface yet — it stays blocked until calibrated uncertainty (#146)
  and the information-architecture decisions (#184/#185/#186) land.
* Every output is **experimental** and carries an uncertainty / confidence band.
  An output is never presented as validated Toledo coaching truth.
* The engine operates on **route / coverage concepts**, never named-player
  claims — low-confidence identity / tracking / calibration must keep the
  language concept-level.
* The V2 diffusion trajectory generator
  (:mod:`pipeline.counterfactual.diffusion_simulator`) is **deferred** — it
  generates no trajectories and ships nothing, it only documents the plan and
  records the NFL Big Data Bowl (#164) offline-pretraining dependency.

Nothing here is wired into ``pipeline.model_router``: it loads no model weights
and runs identical deterministic math regardless of job priority, consuming the
*outputs* of the routed stages — exactly like the frontier analytics and
pre-snap predictor scaffolds.
"""

from __future__ import annotations

from pipeline.counterfactual.diffusion_simulator import (
    DiffusionCounterfactualSimulator,
    DiffusionResult,
)
from pipeline.counterfactual.lookup_simulator import (
    CounterfactualLookup,
    CounterfactualOutcome,
    CounterfactualResult,
    PlayObservation,
    YardsDistribution,
    normalize_concept,
    normalize_coverage,
)

__all__ = [
    "CounterfactualLookup",
    "CounterfactualOutcome",
    "CounterfactualResult",
    "DiffusionCounterfactualSimulator",
    "DiffusionResult",
    "PlayObservation",
    "YardsDistribution",
    "normalize_concept",
    "normalize_coverage",
]
