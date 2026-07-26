# Ball detection model

Play events — throw, catch, interception — cannot be produced without a ball
detection model. This is the one model the pipeline needs that you have to
supply yourself, and this page is how.

## Why this is a separate model

The ball is trained as its own single-class detector rather than as one more
class on the player detector. A frame holds ~22 players and at most one ball,
so a shared head learns to ignore the ball almost entirely — the class
imbalance is severe enough that the ball effectively disappears into the
background class.

## Why an off-the-shelf model will not do

Measured on Toledo drone footage (30 sampled frames, `yolov8n`, confidence
floor 0.05):

| Result | Count |
|---|---|
| `sports ball` detections | 43 |
| …that were actually the football | **0** |
| `horse` / `bird` / `kite` false positives | 55 |

Every `sports ball` hit was a **painted turf arrow**. Doubling the input
resolution changed nothing (42 vs 43 detections, identical confidences), so this
is not a resolution problem: COCO's `sports ball` is a *round* object prior —
soccer balls, basketballs, tennis balls — and a prolate American football does
not match it.

Note the contrast: the same off-the-shelf model detects **players** perfectly
well (~32 per frame, producing tracks with physically correct 6–10 yard
displacements). Only the ball needs custom weights.

## What happens without it

Nothing breaks, and nothing is faked. `get_ball_detector` returns `None`, the
detection strategy records `ball: "disabled"`, and `stage_events` skips its
entire ball state machine — so `event_count` is 0 and no throw, catch or
interception is ever emitted.

This is deliberate. A stub here would inject a fabricated ball into real
footage, and a coach cannot tell an invented throw from a detected one.
Disabled and honest beats populated and wrong.

## Getting weights

Three routes, in rough order of effort:

### 1. A pretrained American-football detector

Roboflow Universe and Hugging Face both host football-specific detectors.
Download the YOLO-format weights (`.pt`) and skip to *Installing* below.

> **Note:** `api.roboflow.com`, `universe.roboflow.com` and `huggingface.co` are
> blocked by the agent network policy in the Claude Code environment, so the
> download has to happen on a machine that can reach them. `github.com` *is*
> reachable, so publishing the weights as a release asset on your own repository
> is a workable way to get them into an automated environment.

### 2. Fine-tune on your own footage

Label the ball in a few hundred frames and fine-tune a nano YOLO. The ball is
15–25 px in this footage, so:

- Train at the native capture resolution — do not downscale.
- Keep the SAHI tiling the pipeline already applies at inference
  (`BALL_TILE = 128`, 0.2 overlap) consistent with training crops.
- Expect to need frames from throws specifically; a ball sitting on the ground
  between plays teaches the model very little.

### 3. Both

Start from a pretrained football detector and fine-tune it on Toledo film. This
is usually the best accuracy per hour of labeling.

## Installing the weights

Put the `.pt` file somewhere the GPU worker can read, and point
`MODEL_BALL_PATH` at it:

```bash
MODEL_BALL_PATH=/models/yolov8n-ball.pt
```

That is the entire integration. The detector, the confidence threshold, the
canonical `ball` class normalisation and the regime-dependent SAHI tiling are
already wired around it.

`.pt` files are gitignored — do not commit weights to the repository.

## Verifying it worked

Run a clip and check the detect artifact:

```bash
python -m pipeline run --input "path/to/clip.mp4" --no-backend --out ./out
```

```jsonc
// out/<clip>/artifacts/detect.json
"detection_strategy": {
  "ball": "sahi-128"   // "disabled" means the weights were not found
}
```

Two things to confirm, in order:

1. `ball` is no longer `"disabled"` — the weights loaded.
2. `ball_detections` in the stage log is greater than zero — the model is
   actually finding something.

The first passing while the second stays at zero means the weights loaded but
the model does not fire on your footage; that is a model-quality problem, not a
configuration one.

Then confirm events appear: `stage_events_done` should report a non-zero
`event_count`, with `throw` and `catch` among the event types.

## Related

- Player detection weights: `MODEL_DETECT_PATH` (defaults to `yolov8n.pt`,
  which ultralytics downloads automatically — no manual step needed).
- Ball tuning constants: `gpu-worker/pipeline/detection/ball_detector.py`.
