# Structured prediction-time context — local continuation

This implements staff context intake, not model qualification. The continuation
remains local and undeployed; numeric protocol fixtures are not football labels.

In Clip Review, open **Fix something → Record position observation**. Choose
Snap, Observed handoff, Observed forward-pass release or Observed catch, select
the actual source track/shot, then enable **Add prediction-time context**. Review
the observed play design, time when context was available, named complete
defenders and task actor/target. Snap context additionally uses the original OL
map, measured LOS/direction, explicit defensive roles/gap alignment, down/distance,
ball position and actual known rush/blitz/protection calls. Empty call selections
only mean reviewed none after the explicit call-context confirmation. Roster
position never establishes an observed role or responsibility.

All required tracks must share the same exact visible native source frame.
Playback between frames cannot be rounded or extrapolated; the source-frame
selector uses actual supplied track timestamps/frame numbers. Entered context
availability uses play seconds and is converted once using the original clip
start. Context-enabled saves use recording timestamp and native frame together.
Context-disabled observations keep their existing clip clock. Unknown source
window, missing/hidden/duplicate frame, future context, conflicting side, unknown
population or incomplete required context prevents that context save and explains
why. Ordinary observations can still be saved with optional context disabled.

The serializer preserves existing fields and optional observed LTE/RTE roles.
Pressure width features use the original LT/LG/C/RG/RT operator; the optional TE
observations retain their own source/role checks. Targets/outcomes after the
prediction clock are not authored into feature context. Catch completion is an
explicit eligibility observation at the catch, not a later YAC label.

Validation on scoped snapshots:

- Four mounted actual correction POST payloads use the shared explicitly numeric
  fixture with clip start 10s, event recording time 12.5s and native frame 375.
  Thirty-four UI cases pass, including missing/future/side/population/frame
  refusal and optional TE preservation; global TypeScript/scoped ESLint pass.
- The parent ran the actual PostgreSQL POST → stored Event GET → independent
  worker extractor test in the disposable completion database. All four task
  snapshots extract from the stored recording clock; no FPS conversion is used.
- The reviewer ran 115 worker cases covering intake, expected outcomes, reviewed
  skill results, lane windows and actor bindings: all pass in 3.61s. Earlier 41
  correction-form cases also pass with the context integration. These counts
  are scoped snapshots, not a complete-release suite or accuracy evidence.

Actual feature coverage, staff targets, frozen whole-source splits, qualified
models and independent production acceptance remain missing. The connected handoff now exposes actual P5 stage snapshots as a hash-bound retrievable artifact in Learning, with current clip revisions and per-job trusted source attestation. The existing target-template/prepare workflow receives that real export; targets and review attestations remain blank until supplied. Processor-only production metric writer checks are implemented and independently reviewed; final combined verification and actual release acceptance remain separate requirements.
