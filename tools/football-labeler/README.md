# Local staff labeling tool

From the repository root, start a server that exposes only this tool:

```powershell
python -m http.server 8030 --bind 127.0.0.1 --directory tools/football-labeler
```

Open <http://127.0.0.1:8030>. Load
`reports/overhaul/labeling-package/sources.json`, enter your staff reviewer ID,
select a source, then choose its exact original film. The browser reads that
local file; this tool sends no film or annotations to a hosted service.

1. Verify the category and independent session/game group. All related clips,
   angles and replays share that group. Export the updated catalog and use that
   same confirmed catalog for every pass and subsequent import.
2. Save boundaries in **original-source seconds**. Enter an observed snap or
   leave it unknown. Give each source window a unique local ID. Reuse the same
   canonical play ID across alternate views/replays; those windows retain their
   separate local IDs and timestamps. Do not infer a new snap from a camera cut.
3. For dense assignments, pause on an actual presented frame, enter target and
   shot IDs, and draw visible body boxes. Change shot ID at camera cuts. Record
   full occlusion/offscreen states without inventing a box. Choose an existing
   track row to edit its identity; leave unknown roster/entity IDs blank.
4. Save each reviewer/pass as its own draft JSON. Browser storage is a recovery
   aid; export backups regularly. Changing reviewer/pass loads its own local
   draft or starts blank. An independent secondary pass must be completed before
   comparison. The separate adjudication pass can compare all source windows.
5. Use the [staff package instructions](../../docs/labeling/README.md) for review,
   adjudication, streaming source hashes and freezing actual human-reviewed
   evidence. The viewer **only exports drafts**. It cannot establish annotation
   accuracy, dense coverage, independence or staff attestation by itself.

The capture buttons and manual boxes require `requestVideoFrameCallback`
presented timestamps, available in current Chrome/Edge. Seeking clears the
previous timestamp; wait for the displayed frame before capturing. Manual time
inputs remain available for recorded source timing. Native video controls may
hide while paused in box-drawing mode; use the separate play/seek controls.

## Actual native-frame stepping

Nominal FPS steps can repeat or skip frames in variable-rate footage. Create a
PTS index for the assigned interval and load it in **Dense assignment**:

```powershell
python tools/football-labeler/index_frames.py --catalog reports/overhaul/labeling-package/sources.json --source source-75c6f29c06e0d83c --start 0 --end 3 --output runs/labeling/practice-assignment-frames.json
```

This reads the original in bounded chunks to calculate its SHA-256 and uses
`ffprobe` to extract actual frame times. It writes a new file and never overwrites
the original or a prior index. Change the source/interval for each actual staff
assignment. With an index loaded, arrow buttons visit its native timestamps;
the recorded box time still comes from the presented frame. Index metadata is
retained in the draft, while the index itself must be saved with the assignment.
An index is not evidence that all visible players/frames were labeled. The
separate dense coverage audit must verify that requirement.

The current indexer deliberately refuses nonzero or unknown video clock origins.
The supplied source inventory has zero-origin video. Offset media need a proven
browser/source-time normalization before they can be densely annotated with this
tool; nominal FPS does not establish that conversion.

## Verification

```powershell
node --test tools/football-labeler/annotations.test.mjs
```

The real-source UI canary records source decode, actual frame PTS, box geometry,
visibility, draft backup/restore and pass separation in
`reports/overhaul/labeler-browser-verification.json`. Its automation-entered
draft labels are test input, not football truth or release evidence.
