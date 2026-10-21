# Real evidence release gate

Run from the repository root:

```powershell
python scripts/evaluate_overhaul_release.py reports/platform-overhaul-release-evidence.json --output reports/platform-overhaul-release-gates.json
python -m unittest discover -s tests_local -p test_overhaul_release.py -v
```

The command exits **1** for missing, invalid or failed evidence; **0** requires
every gate to pass. The checked-in manifest intentionally contains no benchmark
labels or evaluation results. Its generated report is `not_ready`. Unit tests,
the real-media canary and UI wiring do not replace the missing annotations or
establish release accuracy.

All references use `{"path": "relative/or/absolute/path", "sha256": "64 lowercase hex characters"}`.
Relative paths resolve from the manifest's folder. Original media and actual
model bytes are streamed through SHA-256 verification; they are never modified.
Keep weights and private footage outside Git. Do not use a temporary test fixture
as release evidence.

## Source and annotation contract

The manifest has `schema_version: 1`, `sources`, `splits` and `evaluations`.
Each source has a unique `id`, an independent session/game `group_id`, `category`
(`practice`, `coach_game`, `television`), a `media` reference and a `labels`
reference. Separate clips, replays and alternate views from one session/game
share a group. Splits list **group IDs** under `train`, `calibration`, `holdout`.
The evaluator rejects group, identical-media and canonical-play leakage.

A human annotation JSON has:

```json
{
  "evidence_kind": "human_reviewed",
  "reviewed": true,
  "reviewer_ids": ["reviewer-a", "reviewer-b"],
  "source_id": "source-id",
  "media_sha256": "sha256 of original source bytes",
  "plays": [{
    "id": "source-local-play-id",
    "canonical_play_id": "stable-play-across-all-views",
    "start_seconds": 1,
    "snap_seconds": 4,
    "end_seconds": 12,
    "double_labeled": true,
    "adjudicated": true,
    "dense_tracks": [{"target_id": "source-local-target", "points": [
      {"timestamp_seconds": 4, "bbox": [10, 20, 40, 80], "shot_id": "shot-1"},
      {"timestamp_seconds": 4.033, "bbox": [11, 20, 41, 80], "shot_id": "shot-1"}
    ]}],
    "dense_coverage": {"path": "coverage/source-play.json", "sha256": "ACTUAL_SHA256"},
    "identities": [{
      "target_id": "source-local-target",
      "canonical_entity_id": "same-athlete-across-views-of-this-play",
      "player_id": "known-roster-id",
      "readable": true,
      "team_resolved": true
    }]
  }]
}
```

This illustrates keys, not valid release data. Sparse point observations do
not establish density. A play counts toward the 48-play dense benchmark only
with the separate hashed native-frame and human coverage contract below. Double-label acceptance
requires two distinct reviewer IDs and adjudication. Benchmark thresholds are
240 distinct canonical plays, 12 independent groups, balanced as at least 80
plays/four groups per category; 48 dense-track plays, 16 per category; and at
least 20% double-labeled/adjudicated plays.

### Dense native-frame coverage

Each dense play references a JSON `dense_coverage` artifact with:

- `evidence_kind: "human_reviewed_dense_coverage"`, matching `source_id`,
  `media_sha256`, `source_play_id` and `canonical_play_id`.
- Explicit staff attestations `completed`, `all_visible_players_annotated` and
  `occlusion_offscreen_records_reviewed`, each `true`, plus named `reviewer_ids`.
- `native_frame_index: {"path": "...", "sha256": "..."}` referring to an actual
  source-derived `source_frame_index` packet. The index contains matching
  source/hash, positive pixel `width`/`height`, `start_seconds`/`end_seconds`
  covering the play and strictly increasing actual `frame_pts_seconds`.
- `frames: [{"timestamp_seconds": 4, "visible_target_ids": ["target-1"]}, ...]`
  enumerating every assigned native timestamp within the play, including frames
  with no visible targets. Staff must verify all visible players, not select a
  convenient subset of players or frames.

Every visible target requires an actual matching box at that timestamp and a
shot ID. Every known target absent from a frame requires a preceding documented
`occluded` or `offscreen` visibility event not superseded by reacquisition.
Boxes are bounded to the indexed source dimensions. Six-decimal serialization
is allowed; interpolation or reconstruction from nominal FPS is not. A missing
coverage artifact, frame, box or loss record keeps the play outside the dense
benchmark. The evaluator checks these structural contracts and hashes; actual
annotation truth and the native-frame extraction still require independent
review. No script generates human coverage attestations from point count.

## Evaluation artifact contract

Each evaluation entry has a unique `task`, `report` reference and `predictions`
reference. Both JSON artifacts must declare `evidence_source: "real"`.
The prediction artifact has a nonempty `samples` array whose rows identify the
actual `source_id`. Reports declare `evaluator_version`, held-out `source_groups`,
the exact `label_sha256` set for those groups, and `predictions_sha256`. Perception,
geometry, prediction and timing reports also supply `model_weights` references
to the actual evaluated model bytes. This binds metrics to frozen evidence; it
does not independently verify that an evaluator's measurements are truthful.

Required tasks and payloads:

| Task | Report payload and gate |
| --- | --- |
| `segmentation` | `categories[category]`: integer `true_positive`, `false_positive`, `false_negative`, `complete_live_actions`, `total_live_actions`. Precision/recall at least 95%, complete action at least 99% in each category. |
| `events` | `categories[category]`: `snap_absolute_errors_seconds`, `release_absolute_errors_seconds`. Median at most 0.10 s and p95 at most 0.25 s for both events in each category. |
| `tracking` | `categories[category]`: `idf1`, `hota`, `visibility_coverage`, `identity_switches`, `fragmentation`, `visible_player_recall`, `localization_error`. IDF1 at least 90, HOTA at least 70 per category. Coverage and player recall are finite fractions in 0–1; switch/fragment counts are nonnegative integers; localization error is finite and nonnegative. |
| `identity` | Prediction rows: `source_id`, `play_id`, `target_id`, `accepted`, `player_id`. Precision and coverage are recomputed against human labels, with duplicate alternate-view cases counted once. Require 2,000 accepted cases, at least 99.5% precision, two-sided 95% Wilson lower bound at least 99%, and at least 90% readable/team-resolved coverage. |
| `spatial` | `location_absolute_errors_yards`: median at most 0.35 yd, p95 at most 0.75 yd. |
| `clear_lane` | `width_absolute_errors_yards`: p90 at most 0.30 yd; failing this gate permits spacing only. |
| `interactions` | `specialists`: each reports a nonempty `name`, `accepted_precision`, `recall`, `coverage`, `onset_absolute_errors_seconds`. Every included specialist needs at least 95% accepted precision. Precision/recall/coverage are finite fractions in 0–1. |
| `aggregation` | `scenarios` contains passing booleans for canonical counts, alternate views, reprocessing, correction preservation, and every p19 regression named in the evaluator. |
| `gpu_timing` | `gpu_name`, `fast_runs` with `source_minutes`, `height`, `elapsed_minutes`; include a full five-minute 1080p source. p95 completion at most 10 min. `detailed_runs` must include at least 90 source-video min across cameras completed within 480 min. |
| `predictive` | `models`: `completion`, `expected_rushing`, `expected_yac`, `contextual_pressure`; each reports `sample_count`, `loss`, `baseline_loss`, `calibration_error`, `baseline_calibration_error`. Held-out loss must beat the documented baseline and calibration cannot worsen. Record which proper loss was measured in the source report. |
| `production_e2e` | `component_versions` and `checks`, as listed by the evaluator. Each component version is a nonempty string identifying the actual version/hash. The source trace must demonstrate the actual deployed upload-to-result workflow. |
| `workspace` | `checks` for the actual daily coaching and analyst tasks listed by the evaluator, including layout/state preservation, playback, filters, linked evidence, accessible tables and real downloads. |

`aggregation`, `workspace` and `production_e2e` do not require model-weight
references, but still require hashed actual trace artifacts and source/label
lineage. Booleans are rejected as numeric measurements, counts, elapsed timings
or component versions. A boolean check in a report must summarize the supplied real trace, not a
placeholder assertion. Review annotation truth, evaluation methodology, runtime
conditions and representative coverage independently before a production decision.

The output path may not alias the manifest, any supplied evidence, or models
referenced inside an evaluation report, including through hard links. Input or
reference-discovery errors are printed without writing an output file.
