# Governance: roles, visibility modes, and workload gating

Status: implemented in this PR (closes #113 and #114).

This document describes the runtime governance layer that all sensitive
Football-IQ APIs build on.  It is intentionally short: implementation details
live in `app/governance.py` and `app/workload.py`, while this document
captures the *contract* — what callers can rely on and what reviewers should
check when extending it.

## 1. Roles

| Role               | Intent                                              |
|--------------------|-----------------------------------------------------|
| `admin`            | Full platform access; manages users and policy.     |
| `analyst`          | Analytics lead; approves recruiting visibility.     |
| `coach`            | Position / position-group coach.                    |
| `sportsperformance`| Sports performance / S&C / wellness staff.          |
| `player`           | A roster player viewing their own development.      |
| `viewer`           | Read-only external account (recruiting boards, …).  |

Roles live on `users.role` (`app.models.UserRole`).  Convenience FastAPI
dependencies in `app/deps.py` (`require_coach_or_above`, `require_any_staff`,
`require_sportsperformance_or_above`, …) wrap simple role-set checks; the
central RBAC policy below replaces ad-hoc booleans for cross-resource
permissions.

## 2. RBAC policy

`app.governance.POLICY` is the **single source of truth** for which role can
perform which action on which resource.  The posture is **deny-by-default**:
any (resource, action) pair not listed in the table is forbidden.

| Resource              | Action     | Allowed roles                                 |
|-----------------------|------------|-----------------------------------------------|
| `player_profile`      | `read`     | all authenticated roles                       |
| `player_visibility`   | `write`    | admin, analyst, coach                         |
| `player_visibility`   | `approve`  | admin, analyst (recruiting approval)          |
| `player_development`  | `write`    | admin, analyst, coach                         |
| `player_development`  | `approve`  | admin, analyst, coach                         |
| `player_metrics`      | `read`     | admin, analyst, coach, sportsperformance, player |
| `health_workload`     | `read`     | admin, analyst, sportsperformance             |
| `heavy_workload`      | `trigger`  | admin, analyst                                |
| `cfbd_analytics`      | `read`     | admin, analyst, coach, sportsperformance      |
| `counterfactual`      | `read`     | admin, analyst, coach                         |

Routers enforce policies via `require_policy(resource, action)` where the
resource/action matrix is applied. Every denial
emits a structured `audit.access.denied` log line containing only the actor
UUID, role, and the (resource, action) pair — never request payloads.

## 3. Visibility modes (Issue #114)

Player records have an outward-facing **lifecycle state** that controls which
projections they participate in:

* `staff_only` *(default)* — internal only.
* `player_approved` — player may see their own profile.
* `recruiting_approved` — recruiting view (external) is exposed.
* `archived` — hidden from all non-staff projections; staff can still view
  and un-archive.

`GET /api/v1/players` and `GET /api/v1/players/{id}` accept `?mode=` of
`staff` | `player` | `recruiting`.  Modes are validated against the caller's
role (`resolve_visibility_mode`) and the result is shaped server-side
(`shape_player`).  Defense in depth: even if a column is present on the ORM
row, sensitive fields (`metadata`, `user_id`) are stripped before serializing
non-staff projections.  Recruiting omits additional bookkeeping (`is_active`,
`created_at`) so it returns identity facts only.

Transitions are recorded in `player_visibility_audit` and emit
`audit.visibility.changed` log lines.  `PATCH /api/v1/players/{id}/visibility`
is the only mutation surface; recruiting approvals require the
`PLAYER_VISIBILITY:APPROVE` policy (admin or analyst).

Player records whose state excludes them from the requested mode return
`404` rather than `403` so external callers cannot learn whether a record
exists.

### Approval workflow

1. Player record lands in `staff_only` (default).
2. A coach reviews the staff projection and `PATCH`es to `player_approved`
   when the player may see their own profile.
3. An analyst or admin further reviews and `PATCH`es to
   `recruiting_approved` when the profile may be released externally.
4. `archived` removes the record from all non-staff projections.  Staff
   retain read/write access so the record can be revived or audited.

## 4. Health/workload gating (Issue #113)

`app.workload` samples the queued/running `processing_jobs` counts and
classifies the worker pool as `healthy`, `degraded`, or `saturated`.
Thresholds are environment-driven (see
[`.env.example`](../.env.example)):

* `WORKLOAD_QUEUE_THRESHOLD` (default `50`)
* `WORKLOAD_RUNNING_THRESHOLD` (default `20`)
* `WORKLOAD_GATING_DISABLED` (default `false`) — emergency bypass.

`require_workload_capacity("<endpoint-name>")` is a FastAPI dependency
applied to heavy endpoints.  When the snapshot is `saturated` (and gating is
not disabled) it returns:

```json
HTTP/1.1 503 Service Unavailable
Retry-After: 30
{
  "detail": {
    "error_code": "workload_gated",
    "endpoint": "jobs.create",
    "message": "Heavy workload temporarily unavailable due to system load. Please retry shortly.",
    "workload": { "queued": 60, "running": 5, "queue_threshold": 50, "running_threshold": 20, "status": "saturated", "gating_disabled": false }
  }
}
```

A `503` with `error_code=workload_gated` is intentionally distinguishable
from `401`/`403` authorization failures so callers can implement different
retry strategies.  Every decision (allowed or rejected) is emitted as
`audit.gating.allowed` / `audit.gating.rejected` for offline audit.

### Endpoints currently gated

* `POST /api/v1/jobs`
* `POST /api/v1/jobs/{id}/retry`
* `GET /api/cfbd/mac/benchmark` (Issue #163 — cross-conference aggregation)
* `POST /api/v1/self-scout/tendency-break` (Issue #137 — scans the whole
  labeled corpus to (re)generate tendency-break alerts; `require_coach_or_above`
  + `require_workload_capacity("self_scout.tendency_break")`)

`POST /api/v1/jobs` and `POST /api/v1/jobs/{id}/retry` authorize via
`require_any_staff` and apply workload gating independently.
`GET /api/cfbd/mac/benchmark` authorizes via the
`require_policy(Resource.CFBD_ANALYTICS, Action.READ)` gate (coaching
staff only) and additionally applies workload gating because the
benchmark aggregates across the whole conference. The
`heavy_workload:trigger` policy row is reserved for endpoints that
explicitly opt into `require_policy(...)`.

The gating dependency is intentionally cheap and reusable — apply it to
additional heavy endpoints (embedding rebuilds, video re-renders, bulk
exports) as those land.

### System workload status surface

`GET /api/v1/health/workload` returns the current GPU-queue snapshot for
operators.  It is restricted to the `health_workload:read` policy (admin /
analyst / sports-performance) and contains aggregate counters only — no
per-player or per-job identifiers.

> This is *system capacity*, not athlete health data.  The athlete
> health/workload product surface (wellness, GPS/wearables, S&C) is a separate
> concern — see [`health-workload-surface.md`](health-workload-surface.md).

### Athlete health/workload surface (Issue #113)

`GET /api/v1/health-workload/surface` is the role-gated, audit-logged
groundwork for the athlete health/workload product surface.  It is gated by the
same `health_workload:read` policy and returns **no athlete data** — only the
placeholder integration contracts (wellness, GPS/wearables, S&C, all
`not_connected`), the approved-role list, and a non-medical disclaimer.  The UI
is hidden for non-approved roles in both the navigation and the page itself.
The full contract — RBAC, audit events, policy-safe copy rules, and the
integration contracts — lives in
[`health-workload-surface.md`](health-workload-surface.md).

## 5. Audit logging

`audit_event(event, **fields)` in `app.governance` is the single emitter for
governance audit lines.  It hard-limits the fields it will serialize
(`_AUDIT_ALLOWED_KEYS`) and coerces values to scalars to guarantee that
medical data, names, or large payloads never end up in the log stream.

Events to expect:

* `audit.access.denied`
* `audit.visibility.applied`
* `audit.visibility.changed`
* `audit.visibility.mode_denied`
* `audit.visibility.archived_hidden`
* `audit.visibility.player_view_blocked`
* `audit.visibility.recruiting_blocked`
* `audit.gating.allowed`
* `audit.gating.rejected`
* `audit.health_workload.read`
* `audit.health_workload.surface.read`
* `audit.profile.read`
* `audit.profile.upserted`
* `audit.profile.snapshot_created`
* `audit.profile.snapshot_approval_changed`
* `audit.profile.snapshot_approval_blocked`
* `audit.playbook.concept_upserted`
* `audit.playbook.concept_linked`
* `audit.playbook.scored`
* `audit.playbook.score_overridden`
* `audit.playbook.corpus_scored`
* `audit.counterfactual.simulated`

These follow the existing structlog JSON format and can be filtered by
`event=audit.*` in log aggregators.

## 6. Extending the policy

When adding a new sensitive surface:

1. Add a `Resource` member and any new `Action` members to
   `app.governance`.
2. Add the explicit row(s) to `POLICY`.  Default-deny means missing roles are
   already rejected.
3. Use `Depends(require_policy(resource, action))` on the router.
4. For heavy work, additionally apply
   `Depends(require_workload_capacity("module.endpoint"))`.
5. Add tests in `backend/tests/test_governance.py` /
   `test_workload_gating.py` covering the role matrix and the gating
   behaviour.

## 6b. Self-scout tendency breaks & frontier analytics (Issues #137 / #10)

Two coaching-staff-only surfaces build on the layers above:

* **Tendency-break alerts (#137)** are persisted as ordinary `alerts` rows with
  `alert_type=formation_tendency` and `position_group="OFFENSE"` (they are
  offense-wide, not position-specific). They are surfaced via
  `GET /api/v1/self-scout/tendency-break`, which blocks `player`/`viewer` and is
  **not** position-scoped (the generic position filter does not apply to
  team-level scouting). A coach marks one addressed with
  `PATCH /api/v1/alerts/{id}/action` — "actioned" is tracked separately from
  "acknowledged" so staff can see which breaks have been worked.
* **Frontier analytics (#10)** — xSep / xYards / xPressure — are stored as
  `metrics` rows that are **forced experimental and never `analytics_safe`** on
  ingest. `GET /api/v1/analytics/frontier` blocks `player`/`viewer`, returns the
  coach-readable definitions, and labels every value EXPERIMENTAL. See
  [`docs/frontier-analytics.md`](frontier-analytics.md).

## 6c. Player development passport (Issue #7)

The individualized player profile / development passport builds directly on the
visibility lifecycle and RBAC above. Two tables back it
(`player_profiles`, `player_profile_snapshots`); the router is
`app.routers.player_profiles`.

* **Single source of visibility.** The outward-facing lifecycle stays on
  `players.visibility_state` (§3). The passport tables deliberately do **not**
  duplicate it — `shape_player_profile` delegates the staff/player/recruiting
  gate to `shape_player`, so a profile is only visible in a mode the parent
  player record already permits.
* **Private coach notes.** `coach_notes` is staff-only and is stripped from
  every player/recruiting projection regardless of column state. The player
  self-view returns the staff-approved `player_summary`, development goals, and
  curated clips only — never coach notes, identity confidence, or raw metrics.
* **Authoring vs. approval.** Writing profile content and weekly snapshots
  requires `player_development:write` (coaching staff). Approving a snapshot for
  player-facing/recruiting use requires `player_development:approve`. Every
  snapshot records `generated_by` (`manual` / `model_assisted` / `imported`)
  and, once approved, `approved_by` / `approved_at`.
* **Confidence-scored identity (no face recognition).** Each snapshot carries a
  `PlayerIdentityState` (`known` / `probable` / `unknown` / `conflicting` /
  `needs_review`), an `identity_confidence`, and the `identity_signals` that
  produced it (jersey OCR, appearance, trajectory, roster mapping, manual
  correction — never face data). Jersey OCR is never treated as ground truth.
* **Low-confidence guard.** A snapshot whose identity confidence is below
  `PLAYER_PROFILE_IDENTITY_CONFIDENCE_THRESHOLD` or whose state is not
  `known`/`probable` is forced `experimental_flag = True` and **cannot be
  approved** for player-facing use (`409 identity_confidence_too_low`) — a
  low-confidence identity is never silently attached to a player's profile.
  Identity is resolved/corrected via the existing tracklet + coach-correction
  flywheel (`PATCH /api/v1/tracklets/{id}`), not by this router.
* **Audit.** Reads emit `audit.profile.read`; writes emit
  `audit.profile.upserted` / `audit.profile.snapshot_created`; approval emits
  `audit.profile.snapshot_approval_changed` or
  `audit.profile.snapshot_approval_blocked`. All carry identifiers/enums only —
  never profile content, metrics, or notes.

## 6d. Playbook overlays & assignment scoring (Issue #15)

The playbook overlay + execution-scoring surface (`app.routers.playbook`,
`/api/v1/playbook`) builds on the RBAC and workload layers above. See ADR 0004
and [`docs/playbook-overlays.md`](playbook-overlays.md).

* **Coaching-staff only.** A new `Resource.PLAYBOOK` with `read` and `write`
  actions is allowed for admin/analyst/coach. Reading the overlay/score surface
  and authoring concepts/links/scores/overrides are both coaching-staff only —
  execution scores are **never** exposed to player/viewer accounts (Non-Goal:
  no player-facing scores without coach mediation), and sports-performance has
  no tactical-scheme role here. The whole router is additionally guarded by the
  `PLAYBOOK_ENABLED` feature flag (404 when off).
* **Identity-safe, experimental-by-default scoring.** The rule-based engine
  never grades a "wrong assignment" on a low-confidence/conflicting identity (it
  returns `needs_review`), consumes identity/track/calibration confidence, and
  flags outputs experimental until the concept is coach-validated on a `known`
  identity. No face recognition is used.
* **Heavy endpoint is gated.** `POST /concepts/{id}/score-corpus` uses
  `require_workload_capacity("playbook.score_corpus")` and returns the standard
  distinguishable `503 workload_gated` behaviour; per-play scoring is ungated.
* **Corrections feed training.** A coach override (`PATCH /scores/{id}`) is
  authoritative on the row and also writes an `assignment_execution` training
  Label.
* **Audit.** Events carry identifiers/enums only (clip/concept/score ids,
  assignment key, grade, source, counts) — never coaching points, comments, or
  trajectory data: `audit.playbook.concept_upserted` / `concept_linked` /
  `scored` / `score_overridden` / `corpus_scored`.

## 6e. Counterfactual coverage simulator (Issue #141)

The counterfactual simulator (`app.routers.counterfactuals`,
`POST /api/v1/counterfactuals`) is an **offline/backend MVP** built on the RBAC
and workload layers above. There is **no** coach-facing frontend surface — it
stays blocked until calibrated uncertainty (#146) and the IA decisions
(#184/#185/#186). See [`docs/counterfactual-simulator.md`](counterfactual-simulator.md).

* **Coaching-staff only.** A new `Resource.COUNTERFACTUAL` with a `read` action is
  allowed for admin/analyst/coach. The "what-if vs another coverage" surface is
  tactical scheme — never exposed to player/viewer accounts, and (like the
  playbook surface) sports-performance has no role here. The whole router is
  additionally guarded by the `COUNTERFACTUAL_SIMULATOR_ENABLED` dark-launch flag
  (404 when off).
* **Experimental, never trusted truth.** Every response is `experimental` with
  `trusted_for_coaching=false` and prominent uncertainty bands. Caller-supplied
  low-confidence identity/tracking/calibration (or a sparse sample) is recorded
  in `low_confidence_inputs` and forces experimental-only, concept-level
  language — estimates are keyed by route/coverage concepts, never named players.
* **Honest about data.** A route with no measured reps returns
  `data_sufficiency="insufficient"` and no outcomes rather than a fabricated
  number — no mock data is presented as real.
* **Heavy endpoint is gated.** It scans the labeled corpus, so it uses
  `require_workload_capacity("counterfactual.simulate")` and returns the standard
  distinguishable `503 workload_gated` behaviour.
* **Audit.** `audit.counterfactual.simulated` carries route/coverage concept keys,
  the candidate count, and the data-sufficiency tier only — never trajectories or
  player names.

## 7. Integration placeholders

Health and workload data sources beyond `processing_jobs` (GPS/wearables,
S&C platforms, wellness surveys) are out of scope for this batch.  When
those integrations land, they should publish snapshots through the existing
`assess_workload` interface (or a sibling sampler) and remain subject to the
`health_workload:read` policy.
