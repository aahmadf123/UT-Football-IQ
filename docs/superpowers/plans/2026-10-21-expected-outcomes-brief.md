# Expected-outcome implementation brief

This is P5 of `platform-overhaul-spec.md`, following the observed-evidence
producers. Implement executable training/evaluation/inference for completion,
expected rushing gain, expected YAC and contextual pressure. Reuse the existing
pressure classifier/calibrator and model router where equivalent. No weights,
fabricated labels, undocumented hand-tuned estimates, external downloads or new
dependencies. The checkout already has NumPy and experimental pressure models.

All models require actual reviewed targets, qualified feature snapshots at the
prediction time, explicit feature definitions/units and immutable group/media/
canonical-play lineage. Missing targets/features/models abstain. Future outcomes
cannot enter input features. Neighboring footage and alternate views of one play
cannot leak across train/calibration/holdout groups. Validate entire session/game
groups, not frames. Enforce hashes and train/calibration/holdout separation.

Implement typed data extraction/validation, reproducible fitting and persistence,
proper held-out loss/calibration and simple-baseline comparisons, actual inference
and a worker-stage entry point with model routing. Keep models experimental until
artifact-bound held-out evaluation and human promotion are verified. A checkpoint
declaring itself approved is insufficient. Every prediction retains the real
weights hash, feature/version/observation clock and eligible population. No missing
evidence becomes a zero or a baseline presented as a qualified prediction.

Completion/pressure report Brier/log loss and calibration against empirical
training-population baselines. Rushing/YAC report outcome error and calibration
against documented training-population baselines. Never train using holdout.

Expose an actual offline CLI for staff to fit/evaluate after labels arrive; it
must safely refuse an empty/unreviewed dataset, wrong hashes, leakage or overwrites
of evidence inputs. Add an integration contract for the existing worker stage
chain and existing manually queued training workflow; coordinate shared-file
wiring with root. Do not leave an unreachable helper library and call P5 done.

Ownership: new `gpu-worker/pipeline/expected_outcomes/`, a new stage entry point,
training CLI and focused tests. Ask root before changing shared router,
orchestrator, model registry or backend/frontend files; other agents own them.
Use real-code tests first, regression/format/lint checks and self review. Do not
commit, push, deploy or spawn helpers. Write report to
`reports/overhaul/expected-outcomes-continuation.md`; return concise status and
test results plus any remaining implementation or actual-data gates.

Accuracy claims remain blocked by missing staff-reviewed labels. Implement the
working code paths now; do not disguise that external evidence gap as completion.
