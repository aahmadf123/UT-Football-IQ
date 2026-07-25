# Counterfactual coverage simulator (Issue #141)

Status: **offline / backend MVP only** — there is **no** coach-facing "What-if"
frontend yet. Per the [#141](https://github.com/aahmadf123/Football-IQ/issues/141)
readiness review, the coach-facing surface stays blocked until calibrated
uncertainty ([#146](https://github.com/aahmadf123/Football-IQ/issues/146)) and
the information-architecture decisions
([#184](https://github.com/aahmadf123/Football-IQ/issues/184) /
[#185](https://github.com/aahmadf123/Football-IQ/issues/185) /
[#186](https://github.com/aahmadf123/Football-IQ/issues/186)) land.

> *Given a play where route X gained 12 yards against Cover-3, what would have
> happened against Cover-1 or Cover-2?* The simulator answers that question as an
> **experimental, directional** estimate — never a trusted coaching truth.

## What ships now (and what does not)

| Piece | Status |
|---|---|
| MVP lookup + empirical-Bayes engine (`gpu-worker/pipeline/counterfactual/lookup_simulator.py`) | ✅ ships |
| Backend port of the engine (`backend/app/analytics/counterfactual.py`) | ✅ ships |
| `POST /api/v1/counterfactuals` (coaching-staff only, experimental, workload-gated, dark-launch flag) | ✅ ships |
| V2 diffusion trajectory generator (`gpu-worker/pipeline/counterfactual/diffusion_simulator.py`) | ⏸ **deferred** — inert scaffold, generates nothing |
| Coach-facing "What-if" frontend tab | 🚫 **blocked** until #146 + IA |

## Algorithm (MVP)

A deterministic **lookup + empirical-Bayes regression** over historical
`(route_concept × coverage_type) → yards` observations:

1. Bucket every observation into a `(route, coverage)` cell and a per-route
   prior.
2. For a query `(route, coverage)`, blend the cell mean toward the **route**
   prior with a pseudo-count (`PSEUDO_COUNT = 5`): sparse cells lean on the
   prior, well-sampled cells stand on their own.
3. Return an expected-yards distribution: `mean`, `std`, `p10/p50/p90`, a 95%
   confidence band on the mean, a sample size, and a confidence in `[0, 0.6]`
   (capped — the surface is experimental).
4. `simulate(route, factual_coverage, candidate_coverages, top_n=3)` ranks the
   counterfactual coverages by expected yards (best-for-offense first) and
   returns the top *N* with `delta_vs_factual`.

The math is pure stdlib (no numpy/torch), so the GPU-worker engine and the
backend engine are parallel implementations of the same contract — exactly like
`tendency_break_engine` (worker) vs `app.analytics.tendency_break` (backend).

### Honesty rules (no mock data)

- A `(route, coverage)` cell with no samples is **shrunk** toward the route
  prior and flagged `basis="prior"`/`"shrunk"`.
- A **route never seen** is reported `data_sufficiency="insufficient"` with **no
  outcomes** — it is never assigned a number borrowed from unrelated routes (the
  global cross-route average is deliberately *not* a fallback).
- Confidence is hard-capped at `0.6` so a result never *looks* authoritative.
- Estimates are keyed by **route / coverage concepts**, never named players.

### Outcome data is real or absent

The backend builds observations only from **measured** outcomes the platform
already holds:

1. an explicit `yards_gained` / `play_outcome` label, else
2. the measured ball-carrier yardage `pre_contact_yards + post_contact_yards`
   (from `stage_metrics`).

A clip missing a route, a coverage, **or** a measured outcome contributes **no**
observation. When the corpus yields nothing, the endpoint returns
`data_sufficiency="insufficient"` rather than a fabricated distribution.

## Backend endpoint

`POST /api/v1/counterfactuals` — coaching-staff only, experimental, dark-launch.

- **RBAC:** `require_policy(Resource.COUNTERFACTUAL, Action.READ)` —
  admin / analyst / coach only (tactical scheme; never player/viewer, and — like
  the playbook surface — no sports-performance role). See
  [`governance.md`](governance.md) §6e.
- **Dark-launch:** 404s entirely when `COUNTERFACTUAL_SIMULATOR_ENABLED=false`.
- **Workload-gated:** `require_workload_capacity("counterfactual.simulate")` —
  it scans the labeled corpus, so it returns the standard distinguishable
  `503 workload_gated` behaviour under load.
- **American football only:** route/coverage inputs run through the soccer guard
  (`detect_soccer_terms`) → `400 soccer_resource_rejected`.

Every response carries `experimental: true`, `trusted_for_coaching: false`,
`coach_language: "experimental_only"`, the per-outcome uncertainty band, and a
`low_confidence_inputs` list. A caller may pass an optional `input_quality`
block (`identity_confidence`, `tracking_confidence`, `calibration_confirmed`);
any signal at/below `COUNTERFACTUAL_INPUT_CONFIDENCE_THRESHOLD` (or a sparse
sample) is recorded in `low_confidence_inputs` — a shaky identity / track /
calibration substrate never reads as trusted coach-facing language.

### Request / response shape

```jsonc
// POST /api/v1/counterfactuals
{
  "route_concept": "go",
  "factual_coverage": "cover_3",
  "candidate_coverages": ["cover_1", "cover_2_shell", "cover_4"], // optional
  "factual_yards": 12,                                            // optional
  "top_n": 3,
  "video_id": null,                                              // optional film filter
  "input_quality": { "identity_confidence": 0.4, "calibration_confirmed": false }
}
```

```jsonc
{
  "route_concept": "go",
  "factual_coverage": "cover_3",
  "factual": { "expected_yards": 10.2, "confidence_band": [8.1, 12.3], "...": "..." },
  "factual_observed_yards": 12,
  "outcomes": [
    { "rank": 1, "coverage_type": "cover_1", "expected_yards": 18.6,
      "confidence_band": [15.0, 22.2], "p10": 12.0, "p50": 19.0, "p90": 25.0,
      "confidence": 0.42, "sample_size": 8, "basis": "shrunk",
      "delta_vs_factual": 8.4, "experimental": true, "source": "toledo_film" }
  ],
  "data_sufficiency": "sufficient",
  "corpus_size": 142,
  "experimental": true,
  "trusted_for_coaching": false,
  "coach_language": "experimental_only",
  "low_confidence_inputs": ["identity", "calibration"],
  "note": "Experimental estimate from historical reps. Directional only ..."
}
```

## Not routed through the model router

Like the pre-snap predictor (#135/#136), frontier analytics (#10), and the
coverage/pressure classifiers (#139/#140), the MVP simulator loads **no model
weights** and runs identical deterministic math regardless of job priority,
consuming the *outputs* of the routed stages. It registers **no**
`model_router` stage and is absent from `DEFAULT_ROUTING`. See
[`model-routing.md`](model-routing.md).

## V2 (deferred): diffusion trajectory generator

`gpu-worker/pipeline/counterfactual/diffusion_simulator.py` is the documented
home for the V2 diffusion model over player movement, pretrained on the NFL Big
Data Bowl ([#164](https://github.com/aahmadf123/Football-IQ/issues/164),
**offline-only**) and fine-tuned on Toledo data. It is **intentionally inert** —
it loads no weights, imports no torch, and never fabricates a trajectory; calling
it returns a `DiffusionResult(available=False, ...)`. The issue's own risk note
governs this: *diffusion models generate unrealistic trajectories on small data
→ keep behind a feature flag, surface uncertainty prominently.* Promotion
requires a BDB-pretrained checkpoint built offline (carrying the
`offline-pretraining-evaluation-only` marker), Toledo fine-tuning + calibrated
validation (#146), and — if it ever needs the GPU at runtime — a model-router /
registry decision per the "experimental → nightly" rule.

## External-resource note (BDB)

No **new** external resource is introduced. The NFL Big Data Bowl is already
registered (offline-only) in [`LICENSES.md`](../LICENSES.md) and the
[external-resource rubric](external-resource-rubric.md) via #164, which lists
#141 as a downstream offline consumer. BDB tracking is **NFL data, not Toledo
film**: any BDB-derived artifact carries the `offline-pretraining-evaluation
-only` provenance marker and is never presented as a validated Toledo result.

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `COUNTERFACTUAL_SIMULATOR_ENABLED` | `true` | Dark-launch guard; `false` → the surface 404s. |
| `COUNTERFACTUAL_CORPUS_MAX_PLAYS` | `1000` | Cap on clips the workload-gated simulator scans. |
| `COUNTERFACTUAL_INPUT_CONFIDENCE_THRESHOLD` | `0.6` | Caller input-quality at/below this forces experimental-only language. |

## Tests

- `gpu-worker/tests/test_counterfactual.py` — engine estimation, uncertainty
  bands, insufficient-vs-sparse honesty, provenance, deferred diffusion.
- `backend/tests/test_counterfactual.py` — engine + extraction helpers, RBAC,
  dark-launch 404, soccer guard, workload-gated success, low-confidence gating,
  and the honest insufficient-data path.
