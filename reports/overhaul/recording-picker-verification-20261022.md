# Film Room recording picker follow-up

The user identified Film Room's recording picker as the location showing one
drone recording. This frontend-only follow-up removes reproducible hidden
filter/navigation defects. It does not establish the current production film
inventory or prove that every local film was uploaded.

## Diagnosis and resulting behavior

The Review plays picker read the shared dashboard's `data.videos`, which is
already narrowed by saved date and practice/game filters. It then silently
applied a selected-session filter. The session-grouping recording picker also
read that filtered list. URL/Back changes could retain an earlier session or
recording selection; an empty scope could retain the previous video ID.

Both pickers now fetch complete paginated recording catalogs independently of
dashboard date/type filters. Review plays shows its count and session scope,
offers **Show all recordings**, and has separate refresh/retry actions. Unknown
explicit recording links remain unavailable until the user selects another
recording or the requested row is actually uploaded. A valid current selection
survives refresh when newer recordings arrive.

The session-grouping picker states total uploaded and eligible counts, including
which recordings are already grouped here or elsewhere. It preserves the rule
against moving another session's recordings through that control. Empty/error
states explain what is available and provide refresh/retry or the actual upload
entry. Grouping failure does not prevent reading a session. The saved-session
selector also reads every page instead of silently stopping at 100 sessions.

Film Room derives session scope from the current URL. Scope changes clear an
old recording URL while preserving unrelated context; Back and later explicit
video links are honored. Late catalog, clip or session responses cannot replace
the current scope. Practice session creation also hides and clears opponent
input and omits it from the request.

## Film inventory boundary

The previous **30-practice-file / 11,065-frame** detector handoff was offline and
performed **zero production uploads**. The separate staff labeler catalog does
not create Football-IQ API `Video` rows. Film Room lists uploaded API rows.

The retained authored 720p scratch canary proves one drone registration in that
canary: `Dji 20260416110958 0274 D.mp4`, video
`f1523332-1200-478e-9ea9-578fcd871d99`. It does not prove that only one exists in
production, or that no other actor uploaded recordings. The earlier denial of
existing live-browser observation was respected. No production credentials,
browser session, database, processor or deployment was accessed or changed here.

The picker can now expose all returned uploaded recordings. Local-only files
still require actual upload to the chosen library/session. The exact reason
the user's deployed picker currently contains one row remains unverified.

## Final checks and source binding

- **607 frontend tests / 80 files passed** in 67.84s on the final settled source.
- **All 34 Chromium flows passed** in 45.9s, including scope clearing, persisted
  date/type filters, URL/Back, missing recording links and refresh discovery.
- The new session-grouping browser flow uses 205 explicit protocol records,
  loads page two, excludes existing/foreign session members, sends the exact
  selected recording/camera payload and verifies updated membership and reset.
- Global ESLint, TypeScript and production build passed; 17 static pages exported.
- Independent scoped review passed 51 cases. Test-only asynchronous settlement
  cleanup then passed the 18 page/session cases without React `act` warnings.
  The final full unit output retains existing `VideoTransport` harness warnings;
  it is not represented as warning-free.

The early in-progress combined run and first browser attempt remain in `runs/`;
their failures are superseded by the complete frozen-source results above.
Browser endpoints and catalog records are explicit protocol fixtures, with no
real film, staff annotations or production writes. These checks establish UI and
request behavior, not production inventory or football model accuracy.

`recording-picker-verification-20261022.json` binds all **76 changed source,
workflow, test and fixture files** relative to merged base
`88fa8aee5fd09e4607b3ff312d254869bc5b4847`, plus seven retained verification logs. Canonical
source aggregate SHA-256:
`b35f7527975156e1adce06d3e95fb05240af06a505fd877ba8f5e16eadfe0d4c`.
This follow-up changes ten frontend files relative to the previously accepted
68-file snapshot. The other 66 prior files remain byte-identical. Backend and
worker checks were not rerun for this frontend-only delta; their source remains
unchanged. The earlier accepted record is preserved rather than overwritten.

Independent `final_source_log_review: accepted` was recorded at
2026-10-22 10:47 UTC. The reviewer verified the complete 76-file changed set,
all ten frontend delta files, seven recorded log hashes and the preserved prior
record. The other 66 prior source files remain byte-identical, including backend
and worker files. Evidence is `runs/reviewer-recording-picker-final-review.json`.
This accepts the bounded local picker fix and verification record. The fixes
remain local, uncommitted and undeployed. The full twenty-page overhaul and
production/staff/model acceptance remain incomplete.
