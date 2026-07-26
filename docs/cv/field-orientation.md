# Which way round is the field?

A homography fitted to painted lines is determined only up to the symmetries of
the paint. This records what those symmetries cost, which of them can be ruled
out and how, and what is still open.

## The four labellings

The markings of a football field are unchanged by

| symmetry | mapping | orientation |
|---|---|---|
| identity | `(x, y) → (x, y)` | preserving |
| `flip_x` | `(x, y) → (100 - x, y)` | reversing |
| `flip_y` | `(x, y) → (x, -y)` | reversing |
| `rot180` | `(x, y) → (100 - x, -y)` | preserving |

Yard lines are painted symmetrically about the 50, and the hash rows and
sidelines are symmetric about the midline, so all four label the same observed
lines. The DLT fits any of them with identical re-projection error and reports
the same inlier ratio. Nothing downstream can tell which one it got.

The cost is not academic. `flip_y` and `flip_x` swap left and right, so a
receiver aligned to the boundary is reported to the field; `rot180` and `flip_x`
flip the sign of `depth_behind_los`, which is how the coverage graph and the
pressure model tell the offence from the defence.

## Step 1 — handedness rules out the two reflections

`flip_x` and `flip_y` reverse orientation; the identity and `rot180` do not. A
camera on a fixed side of a plane produces an image→plane map of fixed
handedness, and a football camera is always above the field. So the sign of the
projective Jacobian at the frame centre — `det(H) / w³`, where the `w³` term
matters because a fit can carry a negative denominator across the visible image
— is an invariant of any valid calibration.

Verified against synthetic pinhole cameras swept over position, yaw and pitch:
every camera above the plane gives a negative sign, every camera below gives a
positive one. A fit with the wrong sign describes a camera underground.

**On 29 of the 30 Toledo practice clips that fit at all, 15 (52%) came back
mirrored.** More than half of all calibrations had left and right swapped, with
a clean inlier ratio and no reason code.

A mirrored fit is repaired rather than discarded: composing the field-frame
mirror `y → -y` re-labels the same observed lines onto the same landmark set, so
the re-projection error is unchanged. It is a relabelling, not a refit. Which
mirror is used does not matter — `flip_x` and `flip_y` differ by `rot180`,
which is exactly what this step cannot resolve. Fits repaired this way carry
`handedness_corrected`.

## Step 2 — the last bit comes from the players, not the paint

After step 1 the ambiguity is precisely `{identity, rot180}`: one bit. No
geometric evidence closes it, because the field really is symmetric under that
rotation.

The tie is broken by the one asymmetry of an aligned football formation that
does not depend on how the clip was shot: **the defence fields a secondary and
the offence does not.** Both teams put a line on the ball; only one keeps
players ten or more yards off it. So the deeper side of the line of scrimmage is
the defence, and the offence attacks toward it.

Concretely: split the players at the LOS, take the mean depth of the three
deepest on each side, and call the direction toward the deeper side when the two
differ by at least 1.5 yd.

### What it scores

Ground truth was read off the film — on this footage the offence (identifiable
by the yellow non-contact quarterback jersey and the back behind it) attacks
away from the camera in every clip, and the fitted direction of template `+x` in
the image was recorded per clip to convert that into a sign.

| statistic | correct | decided |
|---|---|---|
| mean depth of 3 deepest, margin ≥ 1.5 yd | **13 / 13** | 13 of 18 |
| mean depth of 3 deepest, margin ≥ 1.0 yd | 15 / 15 | 15 of 18 |
| second-deepest player, margin ≥ 2.0 yd | 13 / 13 | 13 of 18 |
| count of players deeper than 9 yd | 16 / 18 | 18 of 18 |
| lateral compactness of the five nearest the LOS | 12 / 18 | 18 of 18 |
| **mean tracklet displacement — the previous method** | **10 / 18** | 18 of 18 |

The method this replaces scored 10 of 18 on a two-way call, which is a coin
toss. It averaged the field-x displacement of every tracklet — both teams,
officials included — over the tracklet's whole span, so a drone pan moved it as
readily as a running back did.

### Voting across frames

A single formation resolves only 13 of 29 clips: the drone crops one team's
depth and the statistic has nothing to compare. Frames are cheap and their
failures are largely independent, so the clip is decided by the frames that did
separate, requiring two thirds of them to agree.

**That resolves 27 of 29 clips and is correct on all 27.** The two refusals are
clips whose frames genuinely split (58% and 51% agreement).

Every consensus threshold from a bare majority to 0.9 is correct on all clips it
decides, so this footage does not choose the number and it is not tuned to it.
Two thirds is where a handful of badly framed frames can no longer outvote the
rest.

## What is still not resolved

- **Absolute field position.** Which end zone the offence is attacking, in the
  sense a coach means it, is still unknown. The template's yard-line offset is
  anchored to a centred span of the template, so `los_x = 30` cannot be read as
  "own 30". That needs a landmark the paint does not carry — numeral OCR or an
  end-zone detection.
- **Refusals are common.** Two of 29 clips end unresolved, and a clip whose
  calibration never reached `analytics_safe` never gets this far at all. An
  unresolved orientation is propagated, not defaulted: `stage_pressure`
  suppresses with `field_orientation_unresolved`, and `stage_coverage` takes the
  same uncalibrated fallback it takes for unconfirmed coordinates.
- **One session, one camera rig.** All 30 clips are one practice, one drone
  operator, one pair of teams. The handedness invariant is geometry and holds
  everywhere; the depth rule is football and should hold anywhere, but 18 clips
  from one session is what it has actually been measured on.

## Reason codes

| code | where | meaning |
|---|---|---|
| `field_orientation_unanchored` | keypoints | the fit is one of the four labellings; narrowed later, not here |
| `handedness_corrected` | calibrate | fit was mirrored and has been reflected back |
| `secondary_depth` / `secondary_depth_vote` | labels | direction measured from formation depth |
| `depth_symmetric` | labels | the two sides did not separate — usually a cropped secondary |
| `one_sided_formation` | labels | one side of the ball is missing from the frame |
| `no_line_of_scrimmage` | labels | no dense cluster to split the formation at |
| `insufficient_resolved_frames` / `frames_disagree` | labels | the clip-level vote refused |
| `field_orientation_unresolved` | pressure | stage suppressed for want of a sign |

## A note on `play_direction`

`stage_labels` used to emit a `play_direction` label on every play, and two
things read it for two different purposes: the orchestrator took its *value* as
the field-frame attacking direction, while the self-scout and tendency-break
engines take its *presence* to mean "this play was a run, and it went this way".
Emitting it unconditionally therefore classified every play as a run.

The orientation it was really computing is now `field_orientation`, which is a
property of the calibration rather than a football label. The run-direction
label is left to whatever can actually tell a run from a pass; nothing in the
worker currently can, so it is not emitted.
