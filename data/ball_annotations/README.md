# Ball annotation manifests (Issue #133)

This directory holds **manifest files only** — the schema and pointers for the
regime-split ball-detection dataset. No image frames, no model weights, and no
real annotations are committed here. The seed dataset (5,000+ frames per
regime) lives in object storage; the active-learning labeling loop writes
manifests that reference those frames by `video_id` + `frame_number`.

## Manifest schema

A manifest is a JSON object validated by
`gpu-worker/pipeline/ball/validate_manifest`:

```json
{
  "version": 1,
  "regime_counts": {"fixed_sideline": 0, "drone_follow": 0},
  "annotations": [
    {
      "video_id": "<uuid>",
      "frame_number": 1234,
      "bbox": [x1, y1, x2, y2],
      "visible": true,
      "regime": "drone_follow",
      "split": "train"
    }
  ]
}
```

- `regime` ∈ `{fixed_sideline, drone_follow}` — gates SAHI at inference
  (`drone_follow` → 128 px tiles, `fixed_sideline` → full frame).
- `split` ∈ `{train, val, test}` — target ratio 70 / 15 / 15.
- `visible: false` marks occluded/blurred balls that are labeled but excluded
  from the recall denominator.
- `bbox` is pixel-space `[x1, y1, x2, y2]`.

See `gpu-worker/tests/test_ball_detector.py` for schema-validation tests.

## What is intentionally NOT here

- Real frames or annotations (privacy + size; kept in the `artifacts` bucket).
- Model weights (`*.pt`) — see `MODEL_BALL_PATH` in `.env.example`.
