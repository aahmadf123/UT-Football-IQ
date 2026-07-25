# Health & Workload surface (Issues #113 + #149 + #9)

Status: groundwork + CV workload-risk signals. The surface is **role-gated,
audit-logged, and policy-safe**. Upstream wellness/GPS/S&C sources remain
documented contracts in `not_connected` state, and Issue #149 added the first
real data: **experimental CV workload-risk signals** (acute:chronic workload
ratio, pose-based gait asymmetry, and a deterministic heuristic risk score)
computed by a nightly rollup — sports-performance indicators for staff review,
never a diagnosis.

This document is the contract for the athlete health/workload product surface:
who may see it, what the UI may say, what it audits, and the placeholder
integration contracts that future feeds must satisfy. It complements
[`governance.md`](governance.md), which owns the platform-wide RBAC/audit
layer.

> **Naming note.** This surface (athlete wellness / training load) is **not**
> the same thing as `GET /api/v1/health/workload` in
> [`governance.md` §4](governance.md), which reports *system GPU-queue capacity*
> for the job-gating layer. They share the `health_workload` policy name by
> history only.

## 1. Role-based access

The surface is restricted to the central RBAC policy
`app.governance.POLICY[(Resource.HEALTH_WORKLOAD, Action.READ)]`:

| Role                | Health & Workload access |
|---------------------|--------------------------|
| `admin`             | ✅ |
| `analyst`           | ✅ (analytics lead)      |
| `sportsperformance` | ✅ (S&C / wellness staff) |
| `coach`             | ❌ |
| `player`            | ❌ |
| `viewer`            | ❌ |

Coaches are intentionally excluded: sports performance is a *parallel* track,
not a coaching view (see `app/deps.py` `require_sportsperformance_or_above`).

### Backend access pattern

`GET /api/v1/health-workload/surface` returns the policy-safe surface status.
It is gated by `require_policy(Resource.HEALTH_WORKLOAD, Action.READ)`:

* approved roles → `200` with the surface status (below);
* any other role → `403` with `error_code: "policy_denied"`.

The payload carries **no athlete PII** — only the viewer's role, the approved-role
list, the placeholder integration contracts, and the disclaimer:

```jsonc
{
  "role": "sportsperformance",
  "data_available": false,
  "disclaimer": "Training-load and wellness context for sports-performance staff only. …",
  "approved_roles": ["admin", "analyst", "sportsperformance"],
  "integrations": [
    { "source": "wellness", "status": "not_connected", "data_categories": ["self_reported_soreness", …], … },
    { "source": "gps_wearables", "status": "not_connected", … },
    { "source": "strength_conditioning", "status": "not_connected", … }
  ]
}
```

Source of truth: `app/health_workload.py` (contracts + `build_surface_status`)
and `app/routers/health_workload.py` (the gated route).

### Workload-risk endpoints (Issue #149)

All under the same `health_workload` policy; the injury-risk view additionally
splits player-level access inside the approved set:

| Endpoint | Gate | Purpose |
|----------|------|---------|
| `GET /api/v1/health-workload/injury-risk` | `require_policy(HEALTH_WORKLOAD, READ)`; **player-level rows only for `admin` + `sportsperformance`** — `analyst` receives position-group aggregates with no player ids/names | Nightly per-player ACWR, gait asymmetry index, heuristic risk score, sprint count, trend series |
| `GET /api/v1/health-workload/daily-loads` | `require_analyst_or_above` (worker read path) | Per-player daily CV load aggregation consumed by the nightly rollup; player-attributed rows only |
| `POST /api/v1/health-workload/daily` | `require_analyst_or_above` (worker write path) | Bulk upsert of `player_workload_daily` rows; **rejects player-attributed rows with identity confidence < 0.70** |

Identity gating: the gpu-worker anonymizes tracklets below the 0.70
identity-confidence floor (`attribution="anonymous_track"`, `player_id=null`),
so low-confidence workload never becomes a named-player row — and the ingest
endpoint re-checks the floor as defense in depth.

> **Placement note.** Issue #149's original file list named
> `backend/app/routers/health.py`, which predates the system-vs-athlete split
> documented above. The athlete injury-risk endpoint deliberately lives here,
> in `health_workload.py`, with the rest of the athlete surface.

### Nightly rollup + alerts

A Cloudflare cron trigger (`workers/wrangler.toml` `[triggers]`, 08:00 UTC)
enqueues a `workload_rollup` job on the nightly queue. The gpu-worker stage
(`pipeline/stage_workload_rollup.py`) recomputes each player's trailing 28-day
daily loads from persisted `workload_fusion` metrics, computes
`ACWR = mean(7-day load) / mean(28-day load)` and the
`acwr-asym-heuristic-v1` risk score (`pipeline/metrics/workload_fusion.py`,
routed via the model registry per #73), upserts `player_workload_daily`, and
fires `workload_risk` alerts when **ACWR > 1.5 or asymmetry index > 1.3**.

**Alert visibility matrix for `workload_risk` (stricter than other types):**

| Role | list | get/ack/action | SSE |
|------|------|----------------|-----|
| `admin`, `sportsperformance` | ✅ | ✅ | ✅ |
| `analyst`, `coach` | filtered out | `404` (existence never leaks) | never fanned |
| `player`, `viewer` | ❌ (all alerts) | ❌ | ❌ |

Enforced in `app/routers/alerts_sse.py` (`RESTRICTED_ALERT_TYPES`,
`alert_type_visible_to`) and `app/routers/alerts.py`.

### Validation

The Issue #149 correlation criterion (asymmetry index vs athletic-trainer
reports, >0.4 Pearson on a 30-player sample) runs against **real Toledo
footage** on staff machines — see
[`asymmetry-validation-runbook.md`](asymmetry-validation-runbook.md) and
`gpu-worker/scripts/validate_asymmetry_correlation.py`.

### Data fusion + dashboard (Issue #9)

Issue #9 adds the governance/integration layer on top of the #149 signals:

**Restricted-context tiers** (`app/governance.py`): the canonical
field→tier mapping (`RESTRICTED_FIELD_TIERS`) defines which health-context
fields are `workload` (admin/analyst/sportsperformance), `rehab`
(admin/sportsperformance), or `medical` (mapped to NO role — declared tier,
no data stored). Per-player escalations live in
`player_profiles.restricted_context_flags` (JSON, migration `0028`) and may
only tighten a field's tier — `effective_field_tier` ignores anything that
would widen access. The flags appear in the staff profile projection only.

**Fusion tables** (migration `0029`, ingest via
`/api/v1/health-workload/ingest/{wellness,gps,strength,academic,injury-history}`,
gated `require_policy(HEALTH_WORKLOAD, WRITE)` = admin + sportsperformance,
every ingest audited with source + count only):

| Table | Tier | Notes |
|---|---|---|
| `wellness_entries` | workload | 1–10 self-report scales; never a clinical assessment |
| `gps_workload_daily` | workload | Catapult-style distances/speeds/accels/player load |
| `sc_sessions` | workload | S&C volume/tonnage/RPE |
| `academic_calendar_events` | — (team-level) | exams/breaks/travel overlay; no per-player academics |
| `injury_history` | rehab | body region/side/status/days missed; **no diagnosis free-text by design** (`extra="forbid"` rejects it on ingest) |

**Dashboard** — `GET /api/v1/health-workload/dashboard` (`app/workload_fusion.py`
does the fusion): unified daily athlete state (CV rollup LEFT-JOIN wellness ×
GPS × S&C on (player, day), academic overlay, tier-gated injury history),
position-group ACWR/load trend series, fatigue flags (unacknowledged
`workload_risk` alerts — sportsperformance/admin only, never coaches or
analysts), source connection status derived from real row counts, and the
in-app policy statement on every response:

> "CV outputs are workload/movement context that support staff judgment —
> they are not a medical diagnosis, and no value on this surface is ground
> truth. Staff decisions always take precedence."

Every fused value carries its `source` and a caveat; missing sources are
explicit `*_:source_unavailable` caveats — never silently absent.
`workload_fusion` metric rows are additionally excluded from the generic
`GET /api/v1/metrics` surface entirely (`HEALTH_WORKLOAD_ONLY_METRIC_NAMES`)
so this RBAC surface is their only read path.

### Frontend gating

The UI is **hidden unless the role is approved**, enforced in two places so a
deep link cannot bypass it:

* **Navigation** — `football-shell.tsx` filters the "Health & Workload" entry
  out for non-approved roles.
* **Page** — the `HealthWorkload` view in `page-renderer.tsx` renders a
  *restricted* notice instead of the surface when the role is not approved. The
  dashboard "Workload & Health" teaser follows the same gate.

Client-side role is resolved by `frontend/src/lib/roles.ts` (`resolveCurrentRole`):

1. the JWT `role` claim when signed in (re-verified by the backend on every
   request — the client gate is *display only*, never a security boundary);
2. a `NEXT_PUBLIC_DEMO_ROLE` override for demos/screenshots;
3. a safe default of `coach`, which is **not** approved — so the surface stays
   hidden until a real session proves an approved role.

To preview the approved-role experience locally, set
`NEXT_PUBLIC_DEMO_ROLE=sportsperformance` (or `analyst` / `admin`).

## 2. Audit logging

Every read of the surface emits a structured audit line via
`app.governance.audit_event`, carrying only the actor UUID, role, and the
`(resource, action)` pair plus a coarse `surface` label — **never** athlete
metrics, names, or other PII (the emitter hard-limits its allow-listed keys).

| Event                                   | When |
|-----------------------------------------|------|
| `audit.health_workload.surface.read`    | A `GET /surface` succeeds for an approved role. |
| `audit.health_workload.injury_risk.read` | A `GET /injury-risk` succeeds (carries `surface`, `date`, `count` — never values). |
| `audit.health_workload.daily_loads.read` | The rollup input aggregation is read. |
| `audit.health_workload.daily.write`     | Nightly rollup rows are upserted (carries `count` only). |
| `audit.access.denied`                   | A non-approved role is rejected by `require_policy`. |

When real data feeds land, each athlete-data read must emit its own
`audit.health_workload.*` event following the same allow-listed-keys discipline.

## 3. Policy-safe UI copy

The surface is sports-performance *context*, not medicine. UI copy must avoid
diagnosis, injury-risk, or return-to-play claims.

**Do**
* "Training-load and wellness context."
* "Supports staff judgement; it does not replace it."
* Mark illustrative/sample values with the mock badge.
* Show the disclaimer (`HEALTH_WORKLOAD_DISCLAIMER`) on the surface.
* Frame CV workload signals as "experimental sports-performance indicators"
  and attach `WORKLOAD_RISK_CAVEAT` to every risk value shown
  ("…not a diagnosis and not a medical prediction of injury").

**Don't**
* "Readiness score", "return-to-play", "diagnosis", "medical".
* Present the workload-risk score as a *prediction* of injury or a clinical
  assessment — it is a review flag over training-load context. The phrase
  "injury risk" appears in UI copy **only** inside the experimental,
  sports-performance-staff-only carve-out with the caveat attached; it is
  never shown to coaches, players, or viewers.
* Surface raw athlete health values without an approved role and audit trail.

The disclaimer string lives once in `app/health_workload.py` (backend) and
`frontend/src/lib/health-workload.ts` (frontend) and is shown verbatim.

## 4. Placeholder integration contracts

Three upstream sources are planned. All are `not_connected`; connecting one is
a future, separately reviewed change that must keep passing the policy/audit
gates above. Categories are deliberately **coarse and non-diagnostic**.

| Source (`source`)         | Display name              | Data categories (planned)                               | Provider examples |
|---------------------------|---------------------------|---------------------------------------------------------|-------------------|
| `wellness`                | Wellness self-report      | self-reported soreness, sleep, energy                   | Team wellness questionnaire; athlete check-in app |
| `gps_wearables`           | GPS / wearables           | total distance, high-speed distance, accelerations, player load | GPS tracking vest; wrist/chest wearable |
| `strength_conditioning`   | Strength & conditioning   | session volume, tonnage, session RPE                    | S&C session log; weight-room tracking sheet |

Wellness data is **self-reported context**, never a clinical assessment.

## 5. Tests

* Backend — `backend/tests/test_health_workload_surface.py`: RBAC gate
  (approved vs. denied roles), policy-safe payload (no PII), and the three
  contracts all starting `not_connected`.
* Backend — `backend/tests/test_health_workload_risk.py`: the injury-risk role
  matrix (player-level vs aggregates vs denied), non-diagnostic copy, audit
  events, and the identity-confidence ingest gate.
  `backend/tests/test_alerts.py` / `test_alerts_sse.py`: the `workload_risk`
  visibility matrix.
* gpu-worker — `tests/test_workload_fusion.py`, `test_gait_asymmetry.py`,
  `test_stage_workload_rollup.py`, `test_workload_risk_alert.py`,
  `test_validate_asymmetry_correlation.py`.
* Frontend — `frontend/src/app/health-workload/health-workload.test.tsx` and
  `frontend/src/lib/roles.test.ts`: nav + page gating per role, the
  workload-risk panel per role, and the role-resolution helpers.
