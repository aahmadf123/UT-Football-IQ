# Field inset readability — local review

Base: `88fa8ae`. No commit, deployment, production write, model download or inference run was performed for this slice.

The synchronized clip inset now has an opaque navy backing, restrained solid green field, brighter marks and outlined actor dots. Its compact width is 24% of the video, bounded to 160–208px; explicit expansion uses 65%, capped at 384px. Always-visible 32px controls move it through four corners, expand/shrink it, or hide it behind a small “Field view” button. Hide/show retains the layout, follows the latest playhead, and restores keyboard focus to the toggle. This does not change video playback.

The exported video map also uses an opaque surface and remains capped at 200px. `pts-overlay-v7-solid-field-inset` prevents reuse of the old 35% blended render contract in new worker runs. Existing published video files are unchanged. Exported pixels have no interactive controls; the clip player provides those controls on its live inset.

Calibration and fresh-track gates remain authoritative. Official, sideline and explicitly unknown person kinds do not become player field dots or counts. A legacy unknown *team* alone does not classify a person as a non-player.

The additionally assigned inspector fix uses recording session/library context: our practice home/away colors become “Our practice group 1/2”, without inventing offense/defense or an opponent. Explicit coherent unit labels remain available; contradictory unit labels are flagged. Referees, sideline people and explicitly unknown people stay outside the Players filter and never display roster names or player positions. A real staff person-kind review is distinguished from a detector candidate. “Review person kind” opens the existing Tag/correction workflow, where staff can correct a candidate to a player. Invalidated filters fall back to All.

## Verification

- 32 focused frontend unit cases passed (8 inset, 24 ClipReview), including native-playhead suppression across a long gap, hide/restore focus, non-player exclusion, practice grouping, conflicting unit evidence, staff-reviewed status, repair action, a refreshed actor pool, and a missing-box interpolation endpoint. Inspector visibility and the in-frame count now use the same bounded sample as the overlay.
- 53 worker field-map/renderer/native-window cases passed, including four full-render calls that exclude explicit non-players while retaining legacy unknown-team player dots; scoped Ruff passed. These were also included in the final 139-case worker scope (25.16s) alongside actor replay and orchestration.
- Frontend typecheck and scoped ESLint passed.
- 3 actual integrated Chromium cases passed in 28.1s at 1440px and 390px: decoded 2s H264/30fps fixture, HTTP byte-range seeking, computed opacity/background, sizing, move/expand/hide/show, updated clock while hidden, unsafe calibration, practice/referee inspection and the reachable person-kind correction form.

Browser media/actor coordinates are explicitly synthetic UI contract fixtures (`SYNTHETIC_UI_TEST_NOT_GAME_FILM`), not football observations. The mock media server initially had no usable seek range; adding proper206/Accept-Ranges responses fixed the test transport, without changing app seeking or weakening assertions. One Windows edit encoding issue was corrected to UTF-8 before the final successful browser run.

## Visual evidence

- [Desktop compact inset](../../runs/field-inset-readability/browser/field-inset-solid-field-in-c881f-d-native-playhead-at-1440px-chromium/field-compact.png)
- [Phone compact inset](../../runs/field-inset-readability/browser/field-inset-solid-field-in-53cff-nd-native-playhead-at-390px-chromium/field-compact.png)
- [Phone expanded inset](../../runs/field-inset-readability/browser/field-inset-solid-field-in-53cff-nd-native-playhead-at-390px-chromium/field-expanded.png)
- [Before: empty schematic style check on saved game frame](../../runs/field-inset-readability/real-frame-empty-field-before.png)
- [After: empty schematic style check on the same frame](../../runs/field-inset-readability/real-frame-empty-field-after.png)
- [Machine-readable render evidence](../../runs/field-inset-readability/render-evidence.json)

The actual saved game preview has unsafe calibration and correctly contained no map. The before/after images apply only the empty drawing surface to a copy, visibly marked “STYLE CHECK ONLY”; they assert no calibrated actors and are not pipeline-publication evidence. Source preview SHA256 `fc8a586dc0905ca47e9a30c155e6268d6eaba876564391c9668baa3a25d46936` remained unchanged. No football accuracy, identity validation, full-game processing or performance gate is claimed.

Independent reviewer accepted the solid inset source, inspector follow-through and native/calibration boundaries. Local screenshot artifacts/traces live under ignored `runs/field-inset-readability/`; the owned port 3128 development server was stopped before releasing build ownership.
