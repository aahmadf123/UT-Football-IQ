# Reviewing position observations in Football-IQ

These instructions describe the platform continuation build. The metadata-only
standalone labeling package remains useful for independent benchmark passes;
it does not automatically map its local target names to platform tracking IDs.
Preserve both original exports. Staff review and source identity must be retained
when evidence moves between tools.

## Record what the film supports

Open a play in Clip Review, select its actual visible player track and choose
**Record position observation** in the correction panel. Use the video to choose
the observation time and source shot. The form preserves the source clock. When
the player, role, team, possession, comparison or outcome is unknown, leave it
unknown. Roster position and nearest-player distance do not establish play roles
or responsibility.

Save player identity separately using the actual roster player. Record original
LT/LG/C/RG/RT roles on the snap when known; those names remain attached to the
original players when they pull or cross. Position participation has its own
opportunity kind. A known route, rush, target, pass-protection or tackle
opportunity may be recorded even when its later result cannot be measured.

| Observation | Staff evidence to record |
| --- | --- |
| Position role/participation | Actual player, observed side and role; opportunity; known interval and attacking direction when applicable |
| Pressure coverage | Actual QB; reviewed pressure/no-pressure state; interval; whether the entire interval was continuously observed |
| Measured LOS snapshot | Original blockers; measured field-coordinate body footprints; all observed defenders and explicit completeness |
| Protection envelope | Measured polygon vertices in boundary order, source time and a consistent envelope name; declared window when known |
| Receiver release/break | Actual receiver and reviewed event time/location |
| Engagement/chip endpoints | Actual blocker/defender; same interaction name across its distinct start/end events |
| Forward-pass target | Actual intended receiver; reviewed aimed forward pass; defender responsibility only when separately established |
| Pass result | Same possession and receiver; actual complete/incomplete/intercepted result; contested/uncontested only when reviewed |
| Offensive reception | Actual offensive receiver; observed completed forward-pass reception |
| Possession endpoint | Same carrier and possession; actual terminal reason and source time |
| Carrier cut | Actual player in possession; reviewed cut time; supported location remains unavailable through a loss |
| Tackle outcome | Actual defender and carrier; made/missed/assist; carrier causation only when separately reviewed |

Reuse a possession name across its target, reception, cut and terminal event. A
handoff or possession start can carry that same name. Reuse an interaction name
only for the same actual blocker/defender interaction. Never enter a guessed zero
for missing measurements or use a result to infer an earlier play call.

## Reprocessing and unresolved observations

Reprocessing can create new tracking IDs. Football-IQ preserves original staff
events and reuses their actor references only when the complete source
observations match uniquely. A changed, missing or ambiguous track requires staff
review. Use the affected saved event's repair action to select the correct actual
current track and source observation; editing an existing event preserves its
history and avoids counting a duplicate annotation. Continue watching the film
while missing measurements remain unavailable.

Multiple valid snaps, releases, catches or handoffs must remain separate. When
they make a prediction clock ambiguous, correct the actual saved tag or remove
only an observation you confirmed was incorrect or duplicated. Removal keeps
the original event, actor and time in Correction history. It excludes the tag
from current measurements and records who removed it. Reprocessing suppresses
the same proposal only with an exact source/actor/native-clock match; changed or
ambiguous evidence produces a visible source-review notice. It never silently
chooses a different player or clock to make a prediction available.

## Predictive training evidence

Prediction-time context, reviewed targets and independent evaluation are separate
inputs. Record only context actually available by the named prediction clock;
do not use a later result to supply missing earlier context. Complete defender
populations and actual rush/blitz/protection calls must be explicitly reviewed.
Unknown context cannot be filled from roster positions or motion.

Actual qualified stage feature exports can generate a blank target CSV through
the commands in
[expected-outcomes-continuation.md](../../reports/overhaul/expected-outcomes-continuation.md).
Staff must fill actual targets, reviewer identities, review times and disjoint
splits before freezing. Independent secondary review and adjudication remain
separate passes. Preparing inputs, passing a synthetic workflow test or manually
reviewing one observation does not qualify an automatic model or satisfy the
240-play release population.

The target-template, prepare and model-training commands require the full
Football-IQ `gpu-worker` checkout and its installed worker environment. Run them
from that directory. This metadata-only package contains the standalone labeling
viewer and benchmark freezer; it does not install the processor or its model
training dependencies. The platform download comes from the deployed Learning
screen after a compatible processor has produced actual feature snapshots.

Keep film, filled player labels and private review exports in the local library
or ignored evidence folders. The package contains no film or model weights.
