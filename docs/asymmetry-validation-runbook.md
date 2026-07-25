# Asymmetry-Correlation Validation Runbook (Issue #149)

Validates the Issue #149 acceptance criterion:

> Asymmetry index correlates (>0.4 Pearson) with athletic-trainer reports
> on a 30-player sample.

The validation must run against **real Toledo footage** — never synthetic
data. The footage lives in secured team storage / the staff machine's local
Drone Footage folder (e.g.
`C:\Users\<user>\OneDrive - University of Toledo\Drone Footage`), so this
run happens **on a machine with access to that folder**, not in CI.

Everything below is an experimental sports-performance validation — CV
asymmetry vs athletic-trainer assessments. It is not a diagnosis and not a
medical prediction of injury.

## Governance (read first)

- **Trainer reports are never committed to this repository** and never
  attached to issues/PRs. Keep the CSV on secured team storage.
- Use roster `player_id` UUIDs, not names, in both CSVs.
- Only numeric severity scores go in the trainer CSV — no clinical notes,
  no diagnosis text.
- Results (`results.json`) contain player ids + numeric pairs; treat them
  with the same care as the inputs.

## Step 1 — Process real clips through the pipeline

Process practice/game clips from the Drone Footage folder through the
standard pipeline (ingest → calibrate → detect → track → reid → **pose** →
metrics). The pose stage persists per-tracklet `pose_stride_symmetry`
metrics (with the `asymmetry_index` scalar) and the metrics stage persists
`workload_fusion` rows; both land in the `metrics` table.

Aim for coverage of **at least 30 distinct players** with **3+ clips each**
inside the assessment window (see Step 3).

## Step 2 — Export the CV asymmetry CSV

Export player-attributed asymmetry rows from the backend database:

```sql
-- cv_asymmetry.csv: player_id,clip_id,clip_date,asymmetry_index,confidence,sample_count
SELECT
  m.metric_value->>'player_id'                 AS player_id,
  m.clip_id                                    AS clip_id,
  DATE(m.created_at)                           AS clip_date,
  m.asymmetry_index                            AS asymmetry_index,
  m.confidence                                 AS confidence,
  COALESCE(m.metric_value->>'sample_count','') AS sample_count
FROM metrics m
WHERE m.metric_name = 'workload_fusion'
  AND m.asymmetry_index IS NOT NULL
  AND m.metric_value->>'attribution' = 'player';
```

Only `attribution = 'player'` rows export — anonymous/low-identity tracks
are excluded by design and must stay excluded.

## Step 3 — Collect the athletic-trainer CSV

Ask the athletic-training staff for one row per player assessment:

```
player_id,assessment_date,trainer_asymmetry_score,laterality
6f9c...,2026-09-12,4,L
```

- `trainer_asymmetry_score`: numeric 0–10 severity (0 = symmetric) or an
  L/R ratio — whichever scale the staff uses, used consistently.
- `laterality`: optional `L`/`R` (recorded for review; the correlation uses
  the magnitude score).

## Step 4 — Run the validator

From `gpu-worker/` (any machine with Python 3.11+, no GPU needed):

```bash
python -m scripts.validate_asymmetry_correlation \
  --cv-metrics cv_asymmetry.csv \
  --trainer-reports trainer_reports.csv \
  --window-days 7 \
  --min-samples-per-player 3 \
  --output results.json
```

On Windows (PowerShell), same command with `python` from the repo venv.

The tool pairs each player's most recent assessment with the
confidence-weighted mean of their CV asymmetry within ±`--window-days`,
computes Pearson r, and prints a verdict:

- **PASS** — ≥30 players paired and r > 0.4 → the acceptance criterion is
  met; attach the r value (not the raw CSVs) to Issue #149.
- **FAIL** — ≥30 players and r ≤ 0.4 → exit code 1. Investigate: pose
  keypoint confidence on the failing clips, window alignment, whether the
  trainer scale is monotonic with asymmetry magnitude.
- **INCONCLUSIVE** — fewer than 30 players paired; collect more clips or
  assessments and re-run.

## Step 5 — Record the result

Comment on Issue #149 (or its follow-up) with: date of run, number of
players paired, Pearson r, pass/fail, and the pipeline model variants used
(`pose` + `workload_fusion` from `processing_jobs.output_artifacts
["model_routing"]`). Do **not** attach the CSVs.
