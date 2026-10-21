# Film annotation codebook

Record visible evidence and preserve uncertainty. The preparation tool supplies
no play boundaries, identity answers, tracking boxes or event guesses.

## Source context and counting

- Confirm the actual session/game from staff records. All extracts, camera
  angles, replays and retranscodes of that session/game share one `group_id`.
  File names and camera switches do not prove independence.
- Use `practice`, `coach_game` or `television` for the observed film category.
  Game footage is not automatically television. Leave the category unconfirmed
  until its format is established. Do not split one session/game across groups
  merely to fill category quotas.
- Give each source-local camera occurrence its own `play.id`. Multiple windows
  may share one `canonical_play_id` when they show the same actual play.
  Retain every occurrence, including replays; mark their relationship in notes.
  Across recordings of one play, reuse the same canonical ID. A camera cut,
  scoreboard card or replay does not itself establish a new snap.
- Use the original recording clock, not seconds since a cut/export or the start
  of a browser review. Within a play, `0 <= start <= snap < end <= duration`.
  Keep the setup, live action and terminal result when visible. Do not invent a
  snap for a window that cannot establish it. Leave its snap null with a reason;
  freezing preserves it under non-countable `sequences`, separately from eligible
  normal plays. Missing snap evidence does not identify a false start or aborted
  rep by itself. Preserve the observed source interval and uncertainty.

## Events

Annotate observed event onset on the original source clock. Snap and release
timing require reviewing adjacent decoded pictures. Preserve camera-loss and
uncertainty notes. A player's proximity to the quarterback does not prove
pressure or contact; only mark football interactions with independently reviewed
visible evidence. Do not turn a disappearance into a release, sack or tackle.

Frame rates are rational metadata. A VFR recording requires presentation time;
do not derive every event from nominal FPS alone. The browser's media clock helps
review, but this manual starter tool does not guarantee presentation-frame
precision or certify the 100 ms/250 ms timing gate. Event benchmark annotation
needs a frame-aware review/adjudication workflow if browser seeking is inadequate.

## Track observations and visibility

- A `target_id` identifies one athlete in one source-local window. Track IDs are
  separate from roster IDs and must be unique within that window.
- Boxes use original-resolution pixel coordinates `[left, top, right, bottom]`,
  with positive extent inside the catalog's width/height. Annotate actual visible
  athlete extent consistently. Canvas display scaling must be converted back to
  original pixels. Do not enter field yards as image pixels.
- Put actual visible boxes into `dense_tracks[].points`, with strictly increasing
  source timestamps and `visibility_state: visible`. Mark loss/occlusion under
  that track's `visibility_events` using `occluded` or `offscreen` and a real
  observed source timestamp. Do not fabricate a box for an offscreen athlete or
  interpolate through a camera cut or loss. Reacquisition requires staff review
  of whether it is the same athlete.
- For the 48 dense-track plays, annotate all visible athletes throughout the
  agreed dense frame schedule and document start/end coverage, missing frames
  and adjudication. The freezer's one-point minimum is only a structural check;
  a few boxes or a sparse identity sample do **not** establish dense coverage.
  Do not count sparse examples as completed dense-track benchmark plays.

A dense benchmark play also needs a separate `dense_coverage` reference
(`{path, sha256}`) to a human-reviewed coverage artifact. That artifact binds the
source hash, source-local/canonical play IDs, named reviewers, reviewed visibility
records, and all actual assigned native frame timestamps to a hashed
`source_frame_index` artifact. Use the [actual frame-index CLI supplied with the
viewer](../../tools/football-labeler/README.md#actual-native-frame-stepping);
never synthesize a uniform FPS grid for VFR footage. Staff must explicitly
attest that all visible athletes on every assigned frame are annotated and that
loss records were reviewed. The labeling/freezing helper never invents this
human coverage artifact. The release evaluator verifies exact frame coverage,
visible-target boxes and loss-state records against those references. A sparse
manual starter draft contributes zero completed dense plays without this evidence.

`dense-coverage-draft.json` is deliberately unreviewed. Its `frames` must enumerate
the actual assigned native timestamps, each with `visible_target_ids`. Populate
source/window identity, hash references and actual reviewer IDs. Only after human
coverage review should staff save a separate immutable artifact declaring
`evidence_kind: human_reviewed_dense_coverage` and the three completed review
attestations true. Hash that artifact and attach its `{path, sha256}` reference as
the reviewed play's `dense_coverage`. Keep paths absolute or relative to the final
release manifest directory. The label freezer preserves that supplied reference;
the release evaluator validates it. A blank template or an unchecked draft never
counts as completed dense review.

## Identity cases

Each identity references an actual annotated `target_id` and explicit
`readable`/`team_resolved` booleans. A known roster identity also requires a stable
`canonical_entity_id` for that athlete in the canonical play. Leave the player
and canonical athlete case null when unresolved. A resolved canonical case is reused across
views of the same play; it is not an unsupported cross-game tracking link.

Record the independently resolved team before assigning a roster `player_id`.
Duplicate jersey numbers are candidates; the same-number opponent must remain
unresolved unless team and player evidence establish the identity. Listed roster
position is a prior, not proof of the on-field role. Use `player_id: null` when
the player is unknown, and explain conflicting/unreadable evidence in notes.
An identity row cannot refer to an absent target or assert a known roster player
while leaving team unresolved.

Keep readable/team-resolved and unreadable/unresolved cases in the annotation
audit. Count the same canonical athlete once across repeated windows. The
2,000-case automatic-identity target is an evaluation of actual accepted model
outputs against reviewed labels; 2,000 boxes or raw jersey reads do not satisfy it.

## Independent review and adjudication

Primary and secondary annotators work separately before comparing. Preserve
their original drafts and reviewer identities. Afterward align occurrence IDs,
compare **all** windows for each canonical play, resolve disagreements against
the source, and save the adjudication pass. A separate adjudicator is preferred;
the primary and secondary must always be distinct people.

Double-label status is assigned only to explicitly selected canonical plays with
all occurrence windows present in both independent passes and adjudication.
Document unresolved disagreement instead of selecting the more convenient
answer. Freeze a new revision after correction; never overwrite earlier evidence.

Human annotation quality, representative source coverage and independent split
design still need staff review. Hashes prevent unnoticed byte substitution; they
do not certify football truth.
