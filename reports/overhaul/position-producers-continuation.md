# Observed position producer continuation

Date: 2026-10-21. Scope: worker producers for overhaul pages 13–16, additive central metric definitions, and the narrow reviewed-event reconciliation needed to carry the evidence. No model approval, publication permission, production deployment, or accuracy claim is added.

## Implemented behavior

| Product | Actual computation and evidence boundary |
| --- | --- |
| Ball holding | Qualified snap to qualified release, sack, or scramble initiation. Continuous source-clock visibility required. A missing terminal event, cut, gap, or hidden sample yields a right-censored supported prefix. |
| First pressure | Qualified complete pressure-observation intervals establish coverage from snap. Earliest pressure survives later retreat. Complete negative coverage is right-censored at release/sack or visibility end; a coverage hole censors at its preceding boundary. A point pressure alone without preceding coverage and any proximity heuristic cannot establish **first** pressure. |
| Tackle-box residence | Snap-defined original LT/RT measured outer body edges, measured LOS, and measured attacking direction define a corridor, unbounded behind LOS. The QB must remain outside continuously for at least 200 ms before release/sack. Duration ends at the first supported outside sample after confirmation; both onset and confirmation are retained. Scramble initiation does not end this separate observation. |
| Rusher proximity | Actual source-time distance to explicitly resolved observed rushers, with missing-sample barriers. Comparison scope is retained and no pressure label is inferred. |
| Clear lane width | At each qualified geometry snapshot, subtract actual field-coordinate body footprints intersecting the original LOS from each original adjacent-center gap region, including explicitly complete defender occupancy. Return the **largest contiguous** free interval, all free intervals, original roles/pair, measurement region, and discrete measured series. Never sum disconnected openings or estimate body width from an image box. |
| Named gaps | Left/right A and B retain original LG/C/RG and LT/LG/RG/RT roles under pulling and reversed attacking direction. C requires an explicitly observed inline tight-end role. Actual runner entry outside the observed outer blocker may be labeled edge; an unknown intended gap remains null. |
| Signed forward gain | Identified carrier from a qualified handoff/possession event, qualified terminal event, supported trajectory and measured LOS/direction. `(carrier endpoint x - original LOS x) * direction`; losses remain negative. This is observed carrier-position gain, not official ball-spot yardage or path length. |
| Handoff to LOS / actual entry | First supported behind-to-forward LOS crossing interpolated only between source-time samples at most 200 ms apart in the same known shot. Retain crossing source time and original gap. Can be measured when the terminal gain is unknown. No crossing becomes an unavailable observation, never completed zero. |
| Pre-/post-contact yards | Signed LOS-to-first-contact and first-contact-to-qualified-endpoint displacement. Require explicit `first_contact`, or `contact` with `first_contact=true`; generic contact is insufficient. Post-contact gain requires the terminal event. |
| Engagement / chip delay | Qualified event pairs sharing interaction and resolved actor IDs; engagement additionally requires the same resolved defender. Closeness cannot create these events. |

The former most-moving-player ball-carrier heuristic is no longer invoked by the production metrics stage. Existing OL proximity hypotheses remain explicitly experimental and are not clear-lane or qualified interaction observations. All newly produced specialist rows retain `experimental_flag=true` and unvalidated provenance. Unsafe calibration cannot emit the new spatial QB or carrier products.

## Input contracts

All numeric times below are **recording/source clock seconds**, not a constant-FPS estimate. Source samples carry `timestamp_seconds`, `shot_id`, `visibility_state`, `field_x/field_y` when available, and optionally an image `bbox` for visibility-only temporal observations. Unknown timestamps, unknown shot continuity, hidden markers, shot changes, and unsupported sample gaps prevent continuity. Spatial coordinates use the existing canonical NCAA field template (goal lines x=0/100, lateral y=±26.665; physical end zones x=-10/110 are supported). LOS must be between the goal lines.

Qualified observation source is human/manual, or an explicitly validated real/model observation with an approved evaluation ID, actual artifact SHA-256, and passed quality checks. A `validated=true` boolean alone is insufficient. Pending, superseded, and suppressed provenance is excluded.

Pressure interval event:

```json
{
  "id": "reviewed-observation-id",
  "event_type": "pressure_observation",
  "source": "human",
  "timestamp_seconds": 10.0,
  "attributes": {
    "timebase": "recording",
    "interval_timebase": "recording",
    "start_seconds": 10.0,
    "end_seconds": 11.2,
    "qb_id": "existing-track-id",
    "pressure_state": "none",
    "coverage_complete": true
  }
}
```

`pressure_state` is `none` or `pressure`, established by the qualified observer. Complete coverage is an explicit reviewed assertion, not inferred from visible player counts. Subsequent intervals may change state. A qualified `pressure` point can establish the onset only within the continuous covered prefix. Release is a terminal boundary: an onset with exactly the same timestamp is not proven to precede release, so the duration is censored there. Zero-length clipped coverage intervals are omitted. Human-event reconciliation preserves distinct observation IDs and normalizes nested interval boundaries when staff use clip clock. Scalar snap/release correction semantics stay intact.

Original front is supplied by qualified snap attributes `los_x`, `attacking_direction`, and `ol_roles` mapping LT/LG/C/RG/RT (optional LTE/RTE) to existing track IDs; explicit `observed_role` + `role_resolved=true` is also accepted. The orchestrator passes measured field orientation as a compatible fallback. Conflicting orientations/roles or inconsistent formation ordering abstain.

Geometry event (at snap or `geometry_observation`) carries:

```json
{
  "body_extents": {
    "existing-track-id": {"min_x": 49.8, "max_x": 50.2, "min_y": 1.6, "max_y": 2.4}
  },
  "defender_occupancy": {"complete": true, "track_ids": ["every-observed-defender-id"]}
}
```

Those extents are measured field-coordinate footprints supplied by reviewed annotation or an approved measurement producer. The complete map must cover all original blockers, all declared defenders, and every known observed defender at that snapshot. The algorithm does not create missing footprints. Image pixel boxes are not substitutes. Series uses discrete snapshots with `series_interpolation=none`.

Carrier events use explicit `attributes.carrier_id` (compatible `ball_carrier_id`/`runner_id`). Contact requires the same carrier and explicit first-contact evidence. Interaction pairs use `interaction_id` and `blocker_id` or `player_track_id`; engagement start/end must identify the same existing defender.

## Output / frontend contract

Every new packet includes `definition_version=2026-10-21.1`, explicit `required_evidence`, actual `event_evidence` with event ID/type/source/timestamp/available shot ID, source interval, and applicable real trajectory/series. Duration packets use `seconds`, `right_censored`, and a retained `endpoint`. Trajectory source intervals are explicitly separate when the displayed path extends beyond a first-onset measurement. Hidden samples and shot IDs remain available for playback barriers.

Central definitions remain in `backend/app/services/metric_definitions.py`:

| Name | Scalar | Unit |
| --- | --- | --- |
| `time_to_first_pressure`, `ball_holding_duration`, `tackle_box_residence` | `seconds` | s |
| `clear_lane_width` | `width_yards` | yd |
| `signed_forward_gain`, `pre_contact_yards`, `post_contact_yards`, `rusher_proximity` | `yards` | yd |
| `handoff_to_los` | `seconds` | s |
| `actual_entry_gap` | `value` (one observed entry), with `gap_label` / `subject_id` | rep |
| `engagement_duration`, `chip_to_route_delay` | `seconds` | s |

`clear_lane_width` includes `gap_label`, `subject_id`, `initial_pair`, `measurement_region={cross_section:"LOS",los_x,y_min,y_max}`, `clear_intervals`, and actual timestamped widths. `tackle_box_residence` includes original corridor bounds/IDs, actual inside/outside samples, and exit onset/confirmation. `signed_forward_gain` includes an observed entry object when available. No extra metric registry or parallel analytics route was introduced.

## Verification

- RED regressions first reproduced absent duration, corridor, clear-lane and signed-gain producers, then the interaction and reconciliation gaps.
- Independent review by `/root/reviewer` led to additional regressions for scramble/corridor separation, current publication state, first-contact evidence, field bounds, same-shot continuity, evidence serialization, and independent entry observation.
- Final full worker suite: **1,430 passed, 1 skipped, 1 deselected** (43.33 s). `ruff check .` passed across the complete worker. The focused producer/reconciliation + overhaul + kinematics selection passed **85 tests**. Additional orchestrator coverage passed in the earlier **91-test** focused run and in the final whole-suite run.
- Central definition test: **1 passed**. Definition files pass Ruff lint, Ruff format check, and mypy (one source file). The isolated definition test performs no database connection.
- Actual film qualification, model evaluation, and production deployment were **not** performed by this producer package.
- Reviewer independently repeated adversarial probes for impossible LOS, conflicting duplicate-timestamp loss markers and interpolation, and pressure exactly at release; all passed. No additional blocking helper defect remained in that review. Final `git diff --check` passed (only existing Windows LF/CRLF notices).

## External gates / next slice

These algorithms intentionally abstain on current point-only tracking when reviewed pressure coverage, original line roles, measured body footprints, complete defender occupancy, carrier/contact events, or stable calibration are unavailable. Supplying placeholder data or flipping validation flags would not implement those missing evidence sources.

Next required work is reviewed source-film annotation and specialist producer/training integration: pressure interval coverage; projected actual body footprints and complete LOS occupancy; blocking engagement/chip/contact/route and responsibility events; carrier/first-contact/ball-spot evidence. Train/evaluate on disjoint real sessions with artifact hashes, approved population slices and human promotion before enabling qualified aggregation. Expected-outcome models (xYards/xPressure/etc.) require separate actual outcome datasets, training, calibration and held-out evaluation; these observed geometry/duration algorithms do not substitute for them.

Atomic machine segmentation-generation replacement and all corrected-window anchoring are separate work owned by root/pipeline during this continuation. They are not implemented by this producer package. The full 20-page plan is not accepted by these unit tests alone.
