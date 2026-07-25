# Eval baselines

Archived `summary.json` outputs from acceptance runs of the turnkey CLI on
real practice footage. They are the reference point for judging future model
or pipeline changes: process the same source clips and compare.

Regenerate (from `gpu-worker/`, CPU is fine):

```bash
python -m pipeline run --input "../Drone Footage/<clip>.mp4" --no-backend --out ./out
```

What to compare, and what to expect:

- `clip_count`, per-clip `tracklet` counts (via the run's console line), and
  `failed_clip_stages` (must stay empty) are the quality signals. Counts are
  not exactly reproducible run-to-run (frame-stride and thread scheduling
  introduce small drift) — treat swings beyond ~10 % as a change worth
  explaining, not noise.
- `capture_regime` / `regime_confidence` / `analytics_safe` describe the
  footage posture. The 2026-07-24 clips classify as `unconstrained` with
  `analytics_safe: false` (indoor practice field; no calibratable yard-line
  map) — a model change that flips these deserves scrutiny.
- `model_routing` records which variants ran; a baseline is only comparable
  against the same routing.
- `stage_timings` are environment-dependent (the baselines were captured on
  a busy 4-core CPU box) — use them for relative shape, never as a perf gate.

| Date | Source clip (`Drone Footage/`) | clips | tracklets |
|------|-------------------------------|-------|-----------|
| 2026-07-24 | Dji 20260416110958 0274 D.mp4 | 1 | 82 |
| 2026-07-24 | Dji 20260416111539 0284 D.mp4 | 1 | 88 |
| 2026-07-24 | Dji 20260416111256 0279 D.mp4 | 1 | 176 |
