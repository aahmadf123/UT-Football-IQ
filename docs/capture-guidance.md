# Capture guidance — getting the most out of any camera

The platform accepts footage from **any** device — a phone on the fence, a
drone over practice, a fixed sideline camera. Nothing below is a requirement;
uploads are never rejected for how they were shot. But some choices decide
which analytics can be computed at all, so here is what each buys you.

## What always works

Player detection, tracking, team split, clip segmentation, and film review
work on any stable footage at 720p or better. Upload from whatever you have.

## Resolution: the ball is the constraint

| Resolution | Players | Jersey numbers | Ball |
|---|---|---|---|
| 720p | good | rarely readable | **not visible** (~15 px, occluded at snap) |
| 1080p | good | sometimes readable | marginal |
| 4K | good | readable when facing camera | trackable in open play |

If ball-dependent analytics matter (throws, catches, ball-carrier metrics),
record at **1080p minimum, 4K preferred**. This is a physics limit, not a
software gap — see `docs/cv/ball-observability.md`.

## Angle and height

- **Elevated beats ground-level.** 10–25° above horizontal separates players
  from each other; ground-level footage stacks them.
- **Drone:** 30–60 m altitude with a 30–60° camera tilt is the sweet spot —
  players stay >40 px tall and the field lines stay visible for calibration.
  Straight-down (nadir) footage defeats jersey OCR; extreme zoom-follow
  defeats field calibration.
- **Fixed sideline:** midfield, as high as the structure allows, wide enough
  to keep line-to-gain context in frame.

## Field calibration (what unlocks spatial metrics)

Speed/distance/separation numbers require the system to see **painted field
lines** — several yard lines and at least one sideline in frame most of the
time. Footage where lines are cropped out or unreadable still gets tracking
and film review, but spatial metrics will be suppressed with an explanation
(never silently).

## Stability and settings

- Lock exposure/focus if the device allows; avoid auto-zoom "follow" modes.
- 30 fps is enough; 60 fps helps ball tracking at 4K.
- Avoid shooting into low sun; backlit players defeat team-color splitting.
- Phone users: landscape, lens wiped, highest resolution the storage allows.

## Practice vs. game

Both are first-class. Tag the session kind at upload (practice/scrimmage/
game) — models and analytics are regime-aware, and the tag is how your
footage improves the system for that context.
