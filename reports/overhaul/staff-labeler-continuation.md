# Connected staff labeling continuation

The supplied staff entry is now `tools/football-labeler/start-labeler.cmd`, opening
`http://127.0.0.1:8042` with the verified practice catalog, actual available
source-bound detection proposals and the new `runs/staff-review-20261021` draft
folder. The previously running port-8030 static server and real label data were
not changed. The parent explicitly authorized this new owned local server.

The first task is visible in the UI: enter a reviewer, choose a recording, mark
and save a rep, then draw or explicitly accept/edit one observed person. Film
opens directly from the registered local original; staff do not import raw JSON
to begin. Playback speed, source-time navigation, one-second jumps and exact
native-frame windows support film review. The film remains beside the scrolling
editor on desktop. Rep IDs are staff-readable; source occurrence IDs are retained
in an expandable section. Unsaved boundary/concept fields have their own message
and discard protection.

## Defects reproduced and corrected

The original application was served statically and had no connected local folder
save/load or automatic catalog/media path. It depended on browser recovery and
manual exports. Independent Chromium verification also reproduced the delivered
`file://` entry failing module loading under browser CORS rules while appearing
to show working controls. The archive now includes the Windows launcher, server,
connected client and updated START-HERE instructions; file-mode entry explicitly
explains the working launch path.

Actual isolated browser testing additionally found and fixed a native fetch
receiver error that blocked draft loading, and a paused-seek callback ordering
case that cleared a newly presented frame timestamp. Programmatic seek lifecycle
now retains the actual presented-frame callback evidence, without substituting
requested time or nominal FPS as annotation time. Typing a different reviewer
before blur/unload can no longer relabel an already loaded draft: attribution
belongs to the loaded source/reviewer/pass rather than mutable form inputs.

## Persistence and evidence contract

The standard-library server binds to loopback only. Host/Origin checks and a
per-process save capability protect writes; originals can only be read through
catalog source IDs. It exposes only the viewer's static assets, never arbitrary
filesystem paths. Original media range requests stream bounded chunks and verify
size/full SHA when available. Moved originals require their actual full hash;
same-name/same-size substitutes are refused. Large originals use streaming server
verification, avoiding full-game browser buffers. Native indexing uses actual
integer frame PTS/time-base before rounded decimal or best-effort fallbacks and
refuses unproven offset/rotation conversion. No uniform frame grid is generated.

Draft saves validate source, hash, reviewer/pass, native image/time bounds and
unreviewed schema. A server-owned revision comparison refuses conflicting tabs.
Each save retains a historical revision before atomically advancing the current
draft, and an OS folder lock excludes another server using that draft directory.
Browser recovery/export remains available when folder saves fail, with explicit
save errors and reload/recovery actions. Imported review flags are reset. Server
saves and exports do not certify independent review, adjudication or dense coverage.

Player/referee/unknown kind and offense/defense/not-applicable/unknown side are
explicit observations. Referees cannot receive player roster identity or
offensive/defensive responsibility. Optional practice period and concept notes
retain staff input only. Purple detection hints remain unreviewed and separate
until staff accept one exact-source-frame box with target/shot/kind/side choices.
Accepted draft points retain proposal provenance. Generic person confidence is
not displayed as referee, side or identity confidence; staff/coaches/spectators
may appear among proposals. No identities, concepts, roles, sides or outcomes
are generated from those hints.

## Verification

- 33 Python tests passed across isolated HTTP/disk persistence, source/hash/path
  negatives, exclusive folder ownership, native frame indexing and existing
  package/freeze behavior.
- Eight JavaScript tests passed for draft invariants, serialized revision saves,
  conflict refusal and source-bound proposal validation.
- Two actual Chromium tests passed against a freshly built and extracted staff
  archive: source playback, real frame inspection/stepping, manual player box,
  explicit referee-hint acceptance, save to disk, reload, reviewer/pass separation,
  un-loaded-reviewer attribution safety, portable export, foreign import refusal
  and file-entry launch explanation. These use only temporary synthetic video
  and numerical annotations, never football truth or real label data.
- Scoped Python Ruff/format, Python compilation, JavaScript syntax and diff
  whitespace checks passed. Independent review reran 14 HTTP/frame cases, eight
  JavaScript tests and both extracted-archive browser tests and accepted this slice.

The authorized actual-practice read-only canary exposed 30 catalog sources.
The first original decoded at 1280×720 for 10.3103 seconds, played from source
time zero, and loaded 309 native hint frames with 11 candidates on its first
frame. The reviewer remained blank, zero saved plays/targets were created, and
the new draft directory contained only its one-byte server lock. Independent
evidence is `runs/reviewer-labeler-real-practice.json` (its local screenshot is
not part of the metadata archive). This proves playback/proposal reachability,
not detection accuracy, reviewed football labels or release acceptance.

## Remaining limits

Actual staff usability acceptance, human source-group confirmation, independent
annotation/adjudication, complete dense coverage and held-out qualification remain
staff work. No real reviewed labels were produced. Proposal quality remains
experimental, and unsupported offset/rotated native clocks fail clearly until an
explicit conversion is supplied. This is a single-computer staff tool, not a
hosted multiuser annotation service. New source code was not committed, pushed
or deployed; the parent owns any final handoff archive regeneration.

## Fresh connected metadata archive

The new archive is
`reports/overhaul/labeling-package/staff-labeling-connected-hints-20261021-final.zip`.
Historical archives are retained. It includes the connected launcher/server/assets,
full-hash catalog and all 30 declared practice proposal files plus their inventory
at the launcher's expected path. It includes no film, weights or reviewed labels.
The optional builder `--proposals` validates catalog membership, full source hashes,
inventory checksums and the actual viewer parser. The exact validated byte snapshots
are archived; missing, modified, reviewed, undeclared, unsafe, case-aliasing or
symbolic-link inputs refuse packaging. No current generator identity is attributed
to historical proposal production.

Final isolated checks comprise 77 local unittest cases (one host-level symlink
case skipped on Windows), 11 actual HTTP server pytest cases, eight JavaScript
cases and two extracted-package Chromium flows. The browser fixtures use generated
protocol video and disposable draft folders. The actual metadata extraction check
validates every manifest entry, starts only the extracted server on a temporary
loopback port, and loads 30 sources / 11,065 hint frames. Zero annotations are
saved and original media is not opened by that packaging check. Exact logs and the
final archive SHA are recorded in `runs/staff-labeler-final-*`; production 8030,
actual staff drafts and backend databases are untouched.
