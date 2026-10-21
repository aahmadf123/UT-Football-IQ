# Format-aware segmentation continuation — 2026-10-21

This is executable engineering progress, **not P1 football-accuracy acceptance**.
The supplied-film smoke run produced no qualified actions or associations. No
trained football snap/terminal/replay model, benchmark, weights or accuracy was
invented or promoted. The overall twenty-page overhaul remains incomplete.

## Implemented

- `film_features.py` decodes actual source frames, audits camera cuts at every
  decoded native presentation timestamp, and samples separate motion features at
  10 Hz. There is no minimum camera-shot duration. A 29.97 FPS regression retains
  cuts at source frames 4 and 7, both below 300 ms.
- Measured field color, white-line support, grass-background affine compensation
  and residual motion supply stable→motion→settled **proposals**. A motion onset
  is never silently labeled a snap. Angle appearance never establishes a fixed
  camera: newly inferred views retain unknown capture regime.
- Cached EasyOCR is optional and downloads are disabled. Raw source-frame/PTS
  readings remain evidence. Repeated complete situations can corroborate angle
  suggestions; replay banners and no-play/penalty text remain unvalidated.
- `film_association.py` separates camera shots, event sequences and canonical
  actions. It handles continuous multiple attempts, short extracts without card
  history, setup/tail context, incomplete action, explicit local nonplays,
  candidate replay banners, changing repeated-image sequences and proposed
  alternate angles. Unknown/nonfield intervals remain reviewable. No assumed
  sideline/endzone ordering fills missing evidence.
- Source start/snap/candidate/terminal/end, confidence, evidence, view,
  association approval status and result version persist in clip label data.
  Three-second setup and two-second post-terminal padding clamp to available
  footage. Fractional PTS remain unrounded.
- Human snap plus actual terminal evidence can qualify an independent action.
  Flat automatic `validated`/model-ID flags cannot. A human-confirmed qualified
  alternate view shares the target's actual `play_call_id` and remains an
  eligible measurement view, with `contributes_snap_count=false`. Replays remain
  statistically excluded. Proposed associations never overwrite independently
  qualified canonical IDs or merge backend plays.
- Any active human annotation revision, human event or manual boundary anchors
  the full existing active layout/IDs at reprocessing entry. Superseded windows
  are excluded. Identity/event corrections on machine-produced clips therefore
  cannot be orphaned by a different cut proposal. Existing manual-window camera
  audits remain intact.
- Online segmentation reads a paginated active-layout snapshot **before**
  inference, collects the entire proposed layout, then makes one atomic batch
  request with the expected active IDs and deterministic generation hash. A
  failure never falls back to individual clip writes. Backend compare-and-swap,
  protected-layout conflict checks and supersession are implemented by the root
  owner. Its 17 real-Postgres boundary lifecycle tests passed, including atomic
  rollback on an injected mid-build failure, CAS, generation retry, source
  coverage validation, confirmed multi-angle canonical count once, and reuse
  of published fast tracks even when no metrics exist.
- Checkpoint identity includes `film-events-2026-10-21.1`; obsolete segmentation
  caches cannot silently bypass the new contract. Generic optical-flow fallback
  windows also remain proposals excluded from canonical denominators.

## Verification

Final focused worker command:

```powershell
cd gpu-worker
.venv/Scripts/python.exe -m pytest tests/test_film_association.py tests/test_film_features.py tests/test_game_film.py tests/test_segment_models.py tests/test_stage_segment_result_state.py tests/test_pipeline_recovery.py tests/test_overhaul_contracts.py -q
```

**144 passed.** Ruff passed on all changed segmentation/checkpoint/backend-client
and orchestrator files and their affected tests. Meaningful new regressions were
observed failing before fixes: missing association implementation, unqualified
automatic approval, local false-start scope, proposal canonical authority,
corrected machine-window retention, shared alternate-view grouping, short cuts,
checkpoint invalidation, overlapping shots and nonfinite event confidence.
Atomic publication tests verify snapshot-before-inference, full payload collection,
single batch submission and no partial individual writes on conflict.

The independent reviewer inspected the new graph/extractor/writer paths and
confirmed the native-frame reset, canonical grouping, candidate authority and
unknown-capture-regime corrections. Root is independently verifying the backend
batch lifecycle and actual API canonical grouping (17 real-Postgres tests passed).
Published-only machine layouts report `data_source=preserved`, rather than
claiming manual boundary provenance. Human-confirmed additional angles whose
target shot contains several actions remain unresolved and cannot add a snap.

Actual read-only supplied-media execution used the existing `.venv-gpu` runtime
and cached OCR, with no model downloads. Summary:
[segmentation-continuation-canary.json](segmentation-continuation-canary.json).

| Source | Duration | Decoded native frames | Shots | Runtime | Qualified actions / associations |
| --- | ---: | ---: | ---: | ---: | ---: |
| First supplied April 16 practice clip | 10.3103 s | 309 | 1 | 23.218 s | 0 / 0 |
| Existing supplied-game excerpt at original 120 s | 30.013317 s | 1,794 | 3 | 47.219 s | 0 / 0 |

The game cut timestamps were 0, 0.583917 and 25.191833 source-excerpt seconds.
Both runs used verified native PTS and retained 100% of their source interval
union. **Interval retention does not measure complete live-action recall.**
Runtime includes optional OCR and is not the requested 1080p p95 throughput test.

## Remaining blockers and exact limits

- No actual reviewed snap/terminal/replay/no-play training/holdout labels or
  qualified football event model are available. The canaries abstained rather
  than supplying the complete automatic football cuts requested by page 8.
  The executable association/evidence pathway is present; semantic automation
  and measured recall/precision are not complete.
- Automatic crowd/graphic/commercial semantic classification is not trained.
  Cached text can identify some commercial/scoreboard context; other nonfield
  content remains unknown, preserved and excluded. This is not full TV-content
  understanding.
- Replay image matching and angle/situation matching are experimental proposals,
  not qualified associations. Human confirmation is the only current authority;
  future automatic qualification must use the shared real artifact/evaluation/
  film-category approval contract, not a worker boolean.
- Only within-recording associations are proposed. Independent recording clocks,
  calibration and tracking identity remain separate. No synchronization,
  triangulation or cross-camera player identity is inferred.
- Unknown/moving views cannot inherit a static field transform merely because
  field lines resemble an endzone or sideline angle. Qualified framewise field
  geometry remains a separate model/evidence requirement.
- The complete 240-play/12-independent-group benchmark, dense 48 cases,
  adjudication, segmentation acceptance thresholds, real-audio media alignment
  and native-1080p throughput remain outstanding. Passing synthetic regressions
  and these short real-media smoke runs do not close those gates.
