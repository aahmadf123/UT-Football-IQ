# Ball observability on Toledo drone footage

**Determination: non-observable for direct ball detection.**
**Date:** 2026-07-26 · **Footage:** `Drone Footage/Dji 202604161*.mp4` (30 clips)

This is the gate check that precedes any ball-model benchmark. If a human cannot
confidently locate the ball and draw a box, there is no evaluation set to build,
no dataset to label, and no model comparison worth running — the benchmark would
be measuring which model hallucinates most agreeably.

## Source

Native DJI export, unmodified. Not slowed, not re-encoded.

| | |
|---|---|
| Resolution | 1280 × 720 |
| Codec / bitrate | h264, 6.72 Mbit/s |
| Frame rate | 29.97 fps |
| Clip length | ~10 s (309 frames) |

At this scale a player is ~100 px tall and a football is **~15 px** on its long
axis.

## Evidence

### 1. The ball is geometrically occluded at the snap

The drone sits behind and above the offense. Through the snap the ball travels
from the ground, between the center's legs, into the QB's hands — the whole path
is behind the bodies of the two players nearest the camera. Frames 100–136 of
clip `0257` at 4× show jersey numbers 6, 50, 65, 73, 74 and 89 crisply legible
and **no ball at any point**. This is not a resolution limit; it is line of
sight.

### 2. Nothing football-coloured and football-sized exists

A football is tan leather: red channel well above green and blue. Turf is the
opposite. Searching every third frame of 10 clips for compact blobs 5–32 px with
`min(R−G, R−B) > 25` returns, as the strongest candidate in each clip:

| Rank of finding | What it actually is |
|---|---|
| score 124, 130, 104 | Red sideline padding |
| score 96, 94 | A **red car** parked past the track |
| score 74, 73, 71, 69 | **Players' bare forearms** |

Not one candidate across ten clips is a football.

The forearm result is the important one. **Bare skin carries the same R>G>B
signature as leather**, and there are 22 players' worth of it in frame. Even a
perfectly tuned colour prior cannot separate a 15 px ball from a 15 px patch of
someone's arm.

### 3. Motion does not surface it either

A ball in flight is the fastest compact object in a football scene, so it should
separate from the player mass under frame differencing. Searching 8 clips for
small (6–34 px), compact, isolated fast movers yields 4,073 candidates. Ranked
by brownness, the top 12 are **all painted turf** — yard lines and hash arrows —
and every one scores **R−G between −15 and −18**, i.e. *greener* than its
surroundings. Across 1,481 candidates in the highest-motion clip, the maximum
brownness is −15.2. Nothing brown, compact and moving exists to be found.

### 4. A trained detector fails too, and fails confidently

The three probes above test *heuristics*. The obvious objection is that a
trained model does not need hand-tuned colour and motion rules, and this
document previously left that objection open. It is now tested, with a resource
already in the tree and no external dependency: `yolov8n.pt`, whose COCO class
32 is `sports ball`.

Run over 80 frames from 10 clips at a deliberately permissive `conf ≥ 0.10`,
three ways — native 1280 × 720, a 2× upscale, and 4× magnified 320 px tiles,
the most generous thing this checkpoint can be given:

| input | sports-ball detections |
|---|---|
| native 1280 × 720 | 82 |
| 2× upscale | 72 |
| 4× on 320 px tiles | 181 |

**It fires about once per frame, up to 0.68 confidence, and it is wrong every
time.** All sixteen of the highest-confidence detections are the painted white
directional arrow beside a yard numeral. Across all 82 native detections:

| | |
|---|---|
| leather-coloured (R > G > B) | **0 of 82** |
| mean R − G over the detection boxes | **−18.6** |
| centred inside a detected person box | 5 of 82 (6%) |

Mean R − G of −18.6 means the average "ball" this model found is *greener than
it is red*. That is turf. The 6% person-overlap figure looks encouraging at a
glance — the detections are not on players, so they are not helmets — but it is
the opposite: they are on the ground, on the paint.

Note where that number lands. The motion probe independently scored its top
candidates at R − G between −15 and −18 and identified them as painted turf.
A trained detector, a colour heuristic and a motion heuristic converge on the
same wrong object from three unrelated directions, and none of them finds a
football, because there is no football in the pixels to find.

This does not prove a football-specific model would also fail — a fine-tune has
a prior for shape and context that COCO's generic ball class does not. What it
does show is that "just use a trained model" is not the missing step, and that a
model reporting detections on this footage should be assumed to be reporting
paint until shown otherwise. Confirming a football-specific model still requires
either `detect.roboflow.com` allowlisted with an API key, or a football `.pt`
checkpoint the worker can read; as of this writing the host returns
`CONNECT tunnel failed, 403` from the agent proxy and no `ROBOFLOW_*` key is set.

## What this means for the benchmark

The planned work is **not worth running on this footage**:

- **Evaluation set** — the labelling rule is `visible: true` only when a human
  can confidently locate the ball. That yields approximately zero positives, so
  recall is undefined and precision is measured against an empty positive class.
- **Public model benchmark** — a model reporting detections here is reporting
  false positives by construction. The 70% recall / 80% precision / ≤0.10 FP-per-
  absent-frame gate cannot be cleared by any model, and failing it would say
  nothing about the models.
- **`toledo-ball-visible-v2` dataset** — 600–1,000 distinct *visible* ball frames
  cannot be sourced from footage where the ball is not visible.
- **RF-DETR vs YOLO11 comparison** — a controlled comparison on unlabelable data
  compares nothing.

This is the "do not proceed with more backbone swapping" branch of the plan,
reached on the evidence rather than after burning the training budget.

### On zero-shot models

SAM 3 and similar promptable models change *what you can ask for* without a
labelled dataset. They do not change *what light reached the sensor*. Asking
"segment the football" of a frame where the ball is behind the center's hip
returns nothing, and asking it of a frame where the ball is 15 px of tan against
a forearm returns the forearm. Zero-shot is the right tool for the moment the
ball becomes visible; it is not a way around occlusion.

## What would change the answer

An earlier draft of this document asked the video team for two things: a higher
capture resolution (at 4K the ball is ~45 px rather than ~15 px, a different
detection problem entirely) and a camera angle that does not put the exchange
behind the two nearest players. **Both are withdrawn.** Per @aahmadf123 there
will be no single angle, height, resolution or recording method to standardise
on, so the pipeline has to cope with whatever arrives. Asking the capture side
to change is not an available move, and this document should not read as though
it is.

That constraint sharpens rather than weakens the finding. What it means is:

1. **This is a determination about *this footage*, not about the program.**
   Capture varies by design, so some future clip may well show the ball — shot
   wider, closer, from the side, or at a higher resolution. Nothing here should
   be compiled into an assumption that the ball is never visible.
2. **So the observability check belongs in the pipeline, not in a document.**
   A clip where the ball is visible and a clip where it is not should be
   distinguished by measurement, the way the field orientation now is, rather
   than by a global constant. `eval/ball_benchmark.py` exists precisely so a
   later capture can be scored against this determination on the same terms.
3. **Accept a different signal for events.** See below. This is the part that is
   actionable today, and it is unaffected by how any future clip is shot.

## Recommended path: events without the ball

Most of what the ball is wanted for does not actually require detecting it.

| Needs the ball | Does not |
|---|---|
| throw / catch / interception events | snap detection (five other signals) |
| ball trajectory, hang time | formation, personnel, alignment |
| | routes, separation, coverage shell |
| | pressure, O-line metrics, workload |

`stage_events` already runs a multi-signal Bayesian snap detector in which the
ball is one input among several. A throw is inferable from QB mechanics and
receiver behaviour without ever seeing the ball leave the hand. That path is
open, needs no new model, and no labelled data.

## Reproducing

The probes behind each figure are throwaway scripts, not committed. The three
checks are: crop the backfield across the snap and look; threshold for
`min(R−G, R−B) > 25` over compact blobs and rank; frame-difference for isolated
compact movers and rank by brownness. All three are a few lines of OpenCV, and
all three should be re-run against any new capture before this determination is
treated as still true.
