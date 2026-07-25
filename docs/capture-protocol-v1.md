# Football-IQ Capture Guide (v2 — any camera, any angle)

## The rule

**There are no capture requirements.** Shoot with whatever you have — a
drone at any height, a phone in the stands, a tripod on the sideline, an
endzone tower, a press-box camera. Any filename, any resolution, any frame
rate. Upload it; the pipeline figures the rest out (it detects the capture
regime from pixels at ingest — no GPS/SRT/IMU needed) and always produces
player detection, tracking, play clips, and rendered overlays.

What varies with footage quality is *how much extra* you get on top of that
baseline. This document is guidance, not a gate: nothing here is enforced,
and nothing below the "always works" line ever blocks processing.

## What you always get (any footage)

- Player detection with bounding boxes and track IDs
- Automatic play segmentation into clips
- Team split (unsupervised color clustering — no roster needed)
- Rendered overlay video for clip review
- Event heuristics (snap detection where the motion signature is visible)

## Better footage → more metrics

| You provide | You additionally unlock |
|---|---|
| Field yard lines / hash marks clearly visible | Field calibration → spatial metrics: routes, coverage, separation, pressure. When lines aren't visible the UI says so explicitly ("spatial metrics unavailable for this camera angle") instead of failing. |
| Higher resolution (1080p+) | Better small-player recall and jersey-number re-identification |
| 30+ FPS | Better tracking through fast plays and event timing |
| Steady framing (gimbal / tripod) | Cleaner tracks, fewer identity switches |
| Whole formation in frame at the snap | Formation/personnel labels and pre-snap analytics |
| Consistent exposure (avoid auto-exposure pumping) | More reliable team color clustering |

## If you're flying a drone (optional tips, not rules)

The original v1 protocol's numbers remain *good advice* for drone pilots who
want maximum metric coverage: ~90–120 ft AGL, 4K/60 where signal allows,
start recording at huddle break, hold a beat after the whistle, keep the
tackle box and near hashes in frame. Follow them when convenient; ignore
them when not — the film still processes.

## Filenames

Anything. `Dji 20260416110000 0257 D.mp4` is as good as
`TOL_20260503_P2_S07_PL04_REDZONE_DRONEA.mp4`. Session metadata (practice
vs game, date, opponent, side of ball) is entered at upload in the app, not
encoded in filenames.

## Upload

Use the app's Film Room → Upload. Film is processed automatically on upload
(the "Process Film" button remains for manual mode and retries). For bulk /
offline ingest there is also the local CLI:

```bash
python -m pipeline run --input /path/to/footage-or-directory
```

## How the pipeline adapts (for the curious)

At ingest, a pixel-only detector classifies the capture regime:

- `drone_follow` — overhead drone following the play → small-player
  detection composition (tiled + dual-resolution inference)
- `fixed_sideline` — fixed elevated sideline/press-box view → full-frame
  detection
- `unconstrained` — anything else (endzone, handheld, phone, tripod…) →
  the generic path with recall-first small-player composition
- `unknown` — the file couldn't be analyzed at all (corrupt/unreadable)

Field calibration is attempted opportunistically on every regime; when it
can't lock on (no visible lines), spatial metrics are suppressed **with a
coach-readable reason** and everything else proceeds. See ADR 0005.

## History

v1 of this document (May 2026) specified mandatory drone altitudes, frame
rates, and a strict naming convention. That posture was retired by ADR 0005:
real capture already violated it and the platform's goal is to accept film
from anyone, shot any way, with zero coordination.
