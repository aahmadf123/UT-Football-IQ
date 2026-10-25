# Staff film review on this computer

Double-click **start-labeler.cmd** in this folder. On the supplied workstation it opens <http://127.0.0.1:8042> with the verified practice catalog and available experimental detection hints, saving into `runs/staff-review-20261021`. The older server on 8030 is untouched. Python 3.11+ is required; keep the launcher terminal running. One running server exclusively owns each draft folder. If 8042 is already serving this workspace, open it instead of starting another copy.

**First task:** enter your real reviewer ID, choose a recording, watch the original, mark and save one rep, then draw a visible person or review one experimental box. Choose Player, Referee or Unknown yourself, and record Offense, Defense, Not applicable or Unknown from actual evidence. No labels are prefilled.

From the repository/package root, an explicit launch is:

```powershell
python tools/football-labeler/server.py --catalog reports/overhaul/labeling-package/sources-content-verified.json --drafts runs/staff-review-20261021 --proposals runs/practice-detection-review-20261021 --library practice --port 8042 --open
```

Omit `--library practice` for all catalog sources. Omit `--proposals` when no hint files exist. No film or weights ship in the metadata archive. Catalog paths must identify this computer's exact originals.

Opening `index.html` with `file://` blocks browser modules; the page explains how to launch instead of silently appearing functional. The older `python -m http.server` route still permits browser imports/exports but has no folder saves, automatic film loading or one-click frame indexing.

## Review and save

1. Enter your reviewer ID and independent pass. Returning workspaces load their own saved draft; switching reviewer/pass never copies another pass's labels.
2. Choose film by filename. The server checks size and, when supplied, full SHA-256 in bounded chunks. Large verification may take a moment. Originals stay read only.
3. Watch with slow playback, source-time seek or one-second jumps. Mark start, optional visible snap and end. Give the source occurrence its local ID and actual canonical play ID shared by related views. Save play boundaries. Unsaved boundary/concept fields are separate from saved observations.
4. Select a saved play, enter a target ID and actual shot ID, pause and draw its visible box. Record explicit actor kind/side, optional roster evidence and actual occluded/offscreen states. Referees cannot receive offense/defense responsibility or a player roster identity. Unknown people remain unknown.
5. Optional practice period and offensive/defensive concept text records only what staff enter; it never infers concepts, roles or outcomes.
6. Changes autosave locally; **Save draft to this computer** also requests a save. Wait for **Saved to local folder**. Every revision remains in its hashed source/reviewer/pass folder. Failed saves stay visible: export a recovery JSON before reloading or closing. Browser recovery is additional help, not evidence of a successful folder save.
7. **Export draft JSON** makes a portable backup. Import validates source identity and bounds and resets review flags. Conflicting tabs cannot overwrite newer saves: export your recovery, reload the saved pass and reconcile explicitly. No automatic conflict merge occurs.

Category/group confirmation requires actual staff records. Related views and replays share a session/game group; filenames do not prove independence. Export the updated catalog to share confirmations or moved paths. Independent secondary work precedes comparison; only adjudication opens another pass for comparison. Follow the [codebook](../../docs/labeling/codebook.md) and [freeze instructions](../../docs/labeling/README.md). Saved drafts and accepted hints never become reviewed release evidence by themselves.

## Moved originals

Paste a moved original's local full path and choose **Verify and open this original**. Only bytes matching the catalog's full hash are accepted. Export the updated catalog to retain that moved path for a later server session. Small originals also work through the file picker, with their known hash checked first. For known-hash files larger than 64 MiB, use the server path action so full games stream rather than buffering into browser memory. Hash-pending catalogs remain unverified drafts and cannot accept hash-bound model hints.

## Actual native-frame stepping

**Load exact frame steps around this time** inspects a short source window with `ffprobe` and verifies original bytes. Arrows then visit actual measured frame PTS; annotation time still comes from the browser's presented-frame callback. Without an index the UI explicitly identifies approximate steps. At an index boundary, load another window rather than inventing frames. Missing ffprobe, decode errors, unsupported rotated/offset clocks or identity mismatches produce an error; no nominal-FPS index is substituted.

For a portable assignment:

```powershell
python tools/football-labeler/index_frames.py --catalog reports/overhaul/labeling-package/sources-content-verified.json --source SOURCE_ID --start 0 --end 3 --output runs/labeling/NEW-frames.json
```

Integer actual frame PTS and stream time base take precedence over rounded decimals and decoder estimates. The indexer currently requires proven zero-origin, unrotated video. Other clocks need an explicit conversion before dense labeling. A frame index never proves all visible athletes were labeled; independent dense-coverage review remains required.

## Experimental detection hints

With `--proposals DIR`, matching `SOURCE_ID.json` loads automatically. Hints require exact source ID/full SHA/native dimensions and increasing actual clocks. Purple dashed boxes are unreviewed hints shown only on their exact source frame. Jump to a hinted frame, select a box, enter target/shot, choose observed kind/side and accept that single box. Redraw or remove the draft observation as needed. Nothing accepts remaining hints or completes review automatically.

Generic person detections may include staff/spectators. Referee suggestions may use an unvalidated stripe heuristic. They prove no identity, side or player role. The viewer omits generic person confidence instead of portraying it as referee/side/name confidence. Accepted points retain proposal ID/provenance and remain unreviewed drafts.

Schema: `schema_version:1`, `evidence_kind:detection_proposals`, `source_id`, `media_sha256`, `width`, `height`, optional `provenance`, and `frames:[{timestamp_seconds,detections:[{proposal_id,bbox,actor_kind,confidence?}]}]`. Boxes are native pixels; kinds are player/referee/unknown. This is candidate review data, never a completed benchmark.

## Developer verification

```powershell
python -m pytest tests_local/test_labeler_server.py tests_local/test_labeler_frames.py -q
node --test tools/football-labeler/annotations.test.mjs tools/football-labeler/connection.test.mjs
node --test tools/football-labeler/browser.test.mjs
```

The browser test uses repository frontend Playwright/Chromium, ffmpeg and `backend/.venv/Scripts/python.exe` (override `LABELER_TEST_PYTHON` elsewhere). It owns a temporary synthetic video/package/server and verifies actual browser save to disk, reload, hint review, exact steps, pass isolation, attribution safety, import/export and the file-entry explanation. It writes no real staff data.

## Connected staff archive

Build a new metadata-only archive with the existing practice hints:

```powershell
python scripts/build_labeling_bundle.py --output reports/overhaul/labeling-package/NEW-staff-package.zip --proposals runs/practice-detection-review-20261021
```

Node.js is required only for packaging hints through the viewer's actual parser.
The builder checks catalog/source hashes, inventory checksums and native-frame box
schema, and rejects undeclared siblings, unsafe paths and symbolic links. It does
not read original film or generate review attestations. The ZIP contains only
validated JSON hints at the launcher's expected path; it includes no film or model
weights. Extract to a new folder, double-click `start-labeler.cmd`, enter your
reviewer name and select a recording. Hints remain unreviewed until individually
accepted or edited, and saved drafts still require independent staff review.
