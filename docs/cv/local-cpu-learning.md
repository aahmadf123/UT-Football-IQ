# Local CPU coverage learning

The worker can fit an experimental NumPy coverage classifier without a GPU or
hosted inference. This trains the existing graph/softmax head, not a new video
detector. No trained candidate is automatically promoted.

Run from `gpu-worker/` using the CPU worker environment:

```bash
python -m training.local_coverage \
  --labels /absolute/path/to/reviewed-coverage-labels.jsonl \
  --features /absolute/path/to/coverage-feature-packets.jsonl \
  --output /absolute/path/outside-git/models
```

The label file contains immutable human truth from reviewed annotation snapshots
and the separately reserved calibration/benchmark labels. Each row requires
`clip_id`, `source: "human"`, `annotated_by`, and `label_type` of `coverage_shell`
or `coverage_tag`. `label_value.coverage` or `label_value.shell` names one of the
existing nine coverage classes. A model prediction is never training truth.

The coverage stage emits `feature_packet` in its artifact only when a
snap, field mapping and attacking direction are available. The orchestrator adds
recording/session lineage. A features JSONL row must contain:

```json
{
  "clip_id": "the-exact-clip-that-was-reviewed",
  "session_id": "practice-day-or-game-session",
  "recording_id": "the-source-recording",
  "split": "train",
  "source": "real",
  "analytics_safe": true,
  "graph": {
    "schema_version": 1,
    "node_ids": [],
    "node_features": [],
    "edge_index": [[], []],
    "edge_features": []
  }
}
```

The empty arrays above illustrate the schema and cannot train a model. Copy the
actual graph from the same clip's artifact. The graph needs at least five
observed defenders with finite features and valid edges. Film with unavailable
field mapping remains useful for review and annotation but cannot supply these
spatial training features. Multiple recordings of one session stay in one split.

Assign `train`, `calibration`, and `holdout` deliberately. The trainer requires
30, 10, and 20 labelled clips respectively, at least two supported classes, and
disjoint sessions across the three splits. Adjacent frames or another recording
of the same session cannot substitute for independent holdout evidence. Only
training graphs update head weights; only calibration graphs fit temperature;
holdout graphs are evaluated afterward. Missing labels/features produce an
explicit `insufficient_data` result and no checkpoint.

The output directory contains a persistent versioned `checkpoint.json`,
`predictions.json`, and completion `report.json`. The report records dataset
hashes, class coverage, split/session counts, holdout accuracy, macro F1, ECE,
and a training-majority baseline. The preliminary release gate requires accuracy
at least 0.90, macro F1 at least 0.85, ECE at most 0.10, and an accuracy gain of
at least two percentage points over that baseline. This gate is additional
evidence; the backend still requires its persisted human-holdout benchmark and
an administrator's promotion before production serving.

Keep generated checkpoints outside Git: the JSON contains learned weights. Use
`COVERAGE_GNN_MODEL` for explicit candidate evaluation. Production coverage
serving resolves the backend's promoted model artifact; an arbitrary local
checkpoint does not imply Toledo validation. A CPU numerical unit fixture is
labelled `synthetic_test`, stays experimental, and cannot pass the release gate.
