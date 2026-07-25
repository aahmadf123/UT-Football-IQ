"""Per-opponent Dirichlet (Beta) run/pass priors (Issue #136).

For a binary run/pass outcome the Dirichlet posterior reduces to a Beta:

    P(pass | opp, down, dist, zone) = (alpha_pass + n_pass)
                                      / (alpha_pass + alpha_run + n_pass + n_run)

Each (opponent, down, distance-bucket, field-zone) cell carries its own
posterior. The default ``Beta(2, 2)`` is a deliberately weak, **uninformative**
prior centred on 0.5 — there are **no hard-coded opponent-specific priors**
(forbidden by the work package). A cell only diverges from 0.5 once real
coach-confirmed outcomes are folded in via :meth:`update`, so every non-default
prior is data-backed and auditable through ``observation_count``.

The store feeds the Bayesian ensemble's base log-odds ``logit(P0)`` and is
persisted server-side in the ``opponent_priors`` table; this module is the
pure-Python in-memory mirror the worker fits and the backend refits overnight.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

# Weak, opponent-agnostic default. Beta(2,2) → mean 0.5, ~2 pseudo-observations
# per class, so two real plays already move the posterior measurably.
DEFAULT_ALPHA_PASS = 2.0
DEFAULT_ALPHA_RUN = 2.0

# A cell needs at least this many real observations before we treat its prior
# as "data-backed" rather than "safely defaulted" for downstream gating.
MIN_OBSERVATIONS_FOR_TRUST = 8

PASS = "pass"
RUN = "run"


def distance_bucket(distance_yards: float) -> str:
    """Bucket yards-to-go into short / medium / long.

    Short ≤ 3, medium 4–7, long ≥ 8 — the standard coaching split also used by
    the self-scout stage's down-and-distance breakdown.
    """
    if distance_yards <= 3:
        return "short"
    if distance_yards <= 7:
        return "medium"
    return "long"


def field_zone(los_x: float | None) -> str:
    """Map a field-frame LOS x (0→100, own goal → opponent goal) to a zone.

    Matches the NCAA field frame from ``docs/calibration-contract.md`` (X in
    yards, end zones excluded). ``None`` (no calibrated LOS) → ``"unknown"`` so
    we never invent a zone.
    """
    if los_x is None or math.isnan(los_x):
        return "unknown"
    if los_x <= 10:
        return "backed_up"  # inside own 10
    if los_x < 50:
        return "own_territory"
    if los_x < 80:
        return "opp_territory"
    return "red_zone"  # inside opponent 20


@dataclass
class DirichletPrior:
    """Beta posterior for a single (opponent, down, dist, zone) cell."""

    alpha_pass: float = DEFAULT_ALPHA_PASS
    alpha_run: float = DEFAULT_ALPHA_RUN
    n_pass: int = 0
    n_run: int = 0

    @property
    def observation_count(self) -> int:
        """Real (non-prior) observations folded into this cell."""
        return self.n_pass + self.n_run

    @property
    def is_data_backed(self) -> bool:
        return self.observation_count >= MIN_OBSERVATIONS_FOR_TRUST

    def pass_probability(self) -> float:
        """Posterior-mean P(pass)."""
        num = self.alpha_pass + self.n_pass
        den = self.alpha_pass + self.alpha_run + self.n_pass + self.n_run
        return float(num / den) if den > 0 else 0.5

    def log_odds(self) -> float:
        """logit(P(pass)) — the base term the ensemble starts its sum from."""
        p = min(max(self.pass_probability(), 1e-6), 1.0 - 1e-6)
        return math.log(p / (1.0 - p))

    def variance(self) -> float:
        """Posterior variance of the Beta — shrinks as observations accumulate."""
        a = self.alpha_pass + self.n_pass
        b = self.alpha_run + self.n_run
        total = a + b
        return float((a * b) / (total * total * (total + 1.0)))

    def update(self, true_class: str, weight: int = 1) -> None:
        """Fold a confirmed outcome into the posterior (the coach flywheel)."""
        if true_class == PASS:
            self.n_pass += weight
        elif true_class == RUN:
            self.n_run += weight
        else:
            raise ValueError(f"true_class must be '{PASS}' or '{RUN}', got {true_class!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "alpha_pass": self.alpha_pass,
            "alpha_run": self.alpha_run,
            "n_pass": self.n_pass,
            "n_run": self.n_run,
            "pass_probability": self.pass_probability(),
            "observation_count": self.observation_count,
            "is_data_backed": self.is_data_backed,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> DirichletPrior:
        return cls(
            alpha_pass=float(d.get("alpha_pass", DEFAULT_ALPHA_PASS)),
            alpha_run=float(d.get("alpha_run", DEFAULT_ALPHA_RUN)),
            n_pass=int(d.get("n_pass", 0)),
            n_run=int(d.get("n_run", 0)),
        )


def _key(opponent: str | None, down: int | None, dist_bucket: str, zone: str) -> tuple[str, str, str, str]:
    return (str(opponent or "unknown"), str(down if down is not None else "unknown"), dist_bucket, zone)


@dataclass
class OpponentPriorStore:
    """In-memory map of situational Beta posteriors, keyed per opponent cell."""

    cells: dict[tuple[str, str, str, str], DirichletPrior] = field(default_factory=dict)

    def get(
        self,
        opponent: str | None,
        down: int | None,
        distance_yards: float | None,
        los_x: float | None,
    ) -> DirichletPrior:
        """Return the posterior for a situation, creating a default cell lazily.

        A freshly-created cell is the safe ``Beta(2,2)`` default — never an
        invented opponent tendency.
        """
        bucket = distance_bucket(distance_yards) if distance_yards is not None else "unknown"
        zone = field_zone(los_x)
        key = _key(opponent, down, bucket, zone)
        if key not in self.cells:
            self.cells[key] = DirichletPrior()
        return self.cells[key]

    def update(
        self,
        opponent: str | None,
        down: int | None,
        distance_yards: float | None,
        los_x: float | None,
        true_class: str,
        weight: int = 1,
    ) -> DirichletPrior:
        """Fold a confirmed outcome into the matching cell and return it."""
        cell = self.get(opponent, down, distance_yards, los_x)
        cell.update(true_class, weight=weight)
        return cell

    def to_rows(self) -> list[dict[str, Any]]:
        """Flatten to JSON rows for persistence in ``opponent_priors``."""
        rows: list[dict[str, Any]] = []
        for (opp, down, bucket, zone), cell in self.cells.items():
            row = {
                "opponent_team": None if opp == "unknown" else opp,
                "down": None if down == "unknown" else int(down),
                "distance_bucket": bucket,
                "field_zone": zone,
                **cell.to_dict(),
            }
            rows.append(row)
        return rows

    @classmethod
    def from_rows(cls, rows: list[dict[str, Any]]) -> OpponentPriorStore:
        store = cls()
        for row in rows:
            down = row.get("down")
            key = _key(
                row.get("opponent_team"),
                int(down) if down is not None else None,
                str(row.get("distance_bucket", "unknown")),
                str(row.get("field_zone", "unknown")),
            )
            store.cells[key] = DirichletPrior.from_dict(row)
        return store
