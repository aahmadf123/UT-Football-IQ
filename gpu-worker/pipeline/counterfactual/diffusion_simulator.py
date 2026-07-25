"""V2 diffusion counterfactual trajectory generator — DEFERRED scaffold (#141).

This is the documented home for the Issue #141 **V2** simulator: a diffusion
model over player movement, pretrained on the NFL Big Data Bowl (#164,
offline-only) and fine-tuned on Toledo data, conditioned on the pre-snap state +
a chosen counterfactual coverage.

It is **intentionally inert**. The readiness review on #141 keeps this surface
offline/backend-only, and the issue's own risk note is explicit:

    "Diffusion models generate unrealistic trajectories on small data → keep
    behind feature flag, surface uncertainty prominently."

So this module ships **no** generation path: it loads no weights, imports no
torch, and never fabricates a trajectory. Calling it returns a structured
``DiffusionResult`` marked ``available=False`` with the reason and the offline
pretraining dependency, so the contract/file exists (per the issue's file list)
without exposing a half-trained generator to coaches. Promotion to a working V2
requires:

* a BDB-pretrained checkpoint built **offline** via ``datasets.bdb`` (#164),
  carrying the ``offline-pretraining-evaluation-only`` provenance marker;
* Toledo fine-tuning + validation (calibrated uncertainty, #146);
* a model-router / registry decision if it ever needs the GPU at runtime, plus a
  ``DEFAULT_ROUTING`` + ``docs/model-routing.md`` + ``test_model_router.py``
  update — it must clear the "experimental → nightly" bar before any
  same-session use.

Until then the MVP lookup engine
(:mod:`pipeline.counterfactual.lookup_simulator`) is the only path that returns
estimates.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Offline-only pretraining source for V2 — mirrors datasets.bdb.schema.USAGE_MARKER.
BDB_PRETRAIN_MARKER = "offline-pretraining-evaluation-only"

REASON_DEFERRED = "v2_diffusion_deferred"
REASON_FLAG_OFF = "v2_diffusion_disabled"
REASON_NO_CHECKPOINT = "v2_diffusion_no_checkpoint"


@dataclass(slots=True)
class DiffusionResult:
    """Result envelope for the deferred V2 generator (never a fake trajectory)."""

    available: bool
    reason: str
    trajectories: list[dict[str, Any]] = field(default_factory=list)
    experimental: bool = True
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "reason": self.reason,
            "trajectories": self.trajectories,
            "experimental": self.experimental,
            "detail": self.detail,
        }


class DiffusionCounterfactualSimulator:
    """Deferred V2 diffusion trajectory generator (Issue #141).

    The constructor records intent only. ``simulate`` always returns an
    unavailable :class:`DiffusionResult` — it generates nothing until a
    Toledo-validated, BDB-pretrained checkpoint is supplied *and* the feature is
    deliberately enabled. This keeps the V2 contract present without ever
    surfacing unrealistic generated trajectories to coaches.
    """

    def __init__(
        self, *, enabled: bool = False, checkpoint_path: str | None = None
    ) -> None:
        self.enabled = enabled
        self.checkpoint_path = checkpoint_path

    @property
    def is_ready(self) -> bool:
        # Even with a checkpoint and the flag on, the generator stays deferred:
        # there is no validated model, and we will not run an unvalidated one.
        return False

    def simulate(
        self,
        pre_snap_state: dict[str, Any] | None = None,
        counterfactual_coverage: str | None = None,
        **_: Any,
    ) -> DiffusionResult:
        """Return a clearly-unavailable result — V2 is deferred, never faked."""
        if not self.enabled:
            reason = REASON_FLAG_OFF
        elif not self.checkpoint_path:
            reason = REASON_NO_CHECKPOINT
        else:
            reason = REASON_DEFERRED
        return DiffusionResult(
            available=False,
            reason=reason,
            detail={
                "v2": True,
                "pretrain_source": BDB_PRETRAIN_MARKER,
                "requested_coverage": counterfactual_coverage,
                "message": (
                    "V2 diffusion counterfactuals are deferred. Use the MVP lookup "
                    "simulator; diffusion stays offline until a Toledo-validated, "
                    "BDB-pretrained, calibrated (#146) checkpoint is promoted."
                ),
            },
        )
