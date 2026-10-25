# Practice, person review and field-view continuation

The new implementation is locally verified against merged base
`88fa8aee5fd09e4607b3ff312d254869bc5b4847`. It is uncommitted and undeployed.
The complete twenty-page overhaul remains incomplete. This record does not
establish improved model accuracy, new trained weights or production acceptance.

## Implemented and reviewed

- Shared recording selectors fetch every API page. Practice context clears an
  opponent, preserves both Toledo units and follows recordings when sessions
  change. Jersey groups do not establish team ownership or an observed unit.
- The calibrated in-clip field inset has an opaque surface, visible controls,
  compact/expanded sizes and move/hide actions. New rendered exports use the
  solid surface; old exports require regeneration after a compatible release.
- Officials have a separate tracking pool. Explicit nonplayers stay out of
  player analysis, field dots, roster attribution and player callouts. Automatic
  stripe-based referee proposals remain unvalidated candidates.
- Staff person-kind and verified manual-identity corrections bind actual source
  observations before player analysis. They survive subsequent identity edits
  and exact reprocessing. Unresolved source/geometry changes hold replacement,
  preserve the prior publication and show a repair warning. General remapping of
  changed candidate trajectories remains open.
- The connected local labeling tool streams registered originals, saves durable
  revisioned drafts, isolates reviewers/passes, exposes failures/conflicts,
  supports native-frame inspection and requires explicit hint acceptance.
- CI now collects the connected server, proposal packaging, connection client
  and extracted-package browser regressions with their required tools.

## Actual film handoff

All **30 practice originals / 11,065 native frames** received local detector
inference at frame step one. The source-bound proposals contain **146,831 person
boxes**, including **16,491 unvalidated stripe-based referee candidates**. Full
source hashes, native-frame counters and all proposal checksums were independently
checked through the actual viewer parser. This was an offline detection handoff;
it uploaded nothing and does not prove full-pipeline or production completion.

The detector weights were not retrained or promoted. No reviewed benchmark labels
were created. The actual inference run preceded the CLI's new generator-source
snapshot field; its outputs are not retroactively attributed to the current CLI
hash. Running inference repeatedly does not train the detector.

The owned reviewer at <http://127.0.0.1:8042> serves all thirty practice sources and
their hints. Independent real-film playback left the reviewer blank and created
no labels. Browser save/reload, hint acceptance and pass isolation were tested
using separate generated protocol film and temporary drafts.

The fresh portable archive is
`labeling-package/staff-labeling-connected-hints-20261021-final.zip`:
2,551,222 bytes, 108 entries, SHA-256
`10e8cc6a053e1c9e46d28d86384d097073dd310eeda8609a9c2912a819db408b`.
It includes the connected tools, full-hash catalog and all thirty unreviewed hint
files, with no film or model weights. Extract into a new folder and run
`tools/football-labeler/start-labeler.cmd`. Original film must remain available
at its catalog path, or be relocated using the exact-source verification action.
Historical archives and real staff drafts are retained.

## Final separate verification runs

| Scope | Observed result |
| --- | --- |
| Backend | 1,054 passed in 205.95s; nine warnings retained |
| Backend static checks | Ruff clean, 237 files formatted, mypy 123 app files clean |
| Worker, Windows | 1,861 passed, one optional skip, four integrations deselected; 91.21s |
| Worker, clean Linux CI image | 1,861 passed, one optional skip, four integrations deselected; 57.05s; Ruff clean |
| Frontend unit | 583 passed across 79 files; 83.55s |
| Frontend Chromium | All 31 flows passed in 56.5s |
| Frontend release checks | Global ESLint, TypeScript and production export passed; 17 static pages |
| Actual local API/worker HTTP | Four integration cases passed in 96.87s; four warnings, 20 helpers/nonintegration cases deselected |
| Local tooling unittest | 77 cases: 76 passed, one Windows symlink-permission skip |
| Connected server pytest | 11 passed |
| Labeling JavaScript | Eight passed |
| Extracted-package Chromium | Two passed with isolated protocol film/drafts |
| Fresh archive | All 106 manifest entries verified; extracted API listed 30 sources and 11,065 hint frames, with no saved annotations |

These are separate runs, with overlapping coverage; their counts are not added.
The initial broad browser run passed thirty flows and failed an obsolete upload
label locator. The updated test checks the actual optional practice focus,
absence of an opponent and null opponent in the request. The complete rerun
passed. The original failing log remains in `runs/`.

The optional worker skip is the custom joblib coefficient fixture. Backend
warnings include deprecations and a recurring asyncpg cancellation coroutine
warning also present in earlier logs; its allocation origin remains unresolved.
HTTP warnings concern the existing EasyOCR/PyTorch quantization path. No warnings
were silently suppressed or represented as evidence of model quality.

The clean Linux snapshot contains 635 files; its manifest hash is
`fa996b3bbb7386c2e46aa96f948c2402250688c0f6647949edb1559d4a25f450`.
Its worker files still match the final source. Later archive tooling changes
have their own final checks above. The snapshot was copied from a read-only
mount into a writable, network-isolated container with no GPU, film or database.

## Source and review boundary

The machine record
`practice-continuation-verification-20261021.json` binds **68 changed source,
test, workflow and fixture files** and the final logs. Its canonical aggregate
SHA-256 is
`b681fce8aa1b8d27e98cca25b24de9d67fcf0492de26e61f021e2901d27b3d3a`.
Scoped implementation reviews and independent regressions passed.
`final_master_hash_review: accepted` was recorded independently at
2026-10-22 00:44 UTC. The reviewer matched the complete 68-file changed source
set, all ten master logs, all 635 retained Linux snapshot files and all 360
current worker files, including the 22 changed worker files. The later labeler
README change has separate final tooling checks. Archive CRC, all 106 manifest
hashes and all thirty proposals passed the actual archived viewer parser.
Independent records are `runs/reviewer-practice-final-master.json` and
`runs/reviewer-labeler-final-archive.json`. This accepts the bounded local
verification record; it does not accept the complete overhaul or production.

Public frontend/API checks served merged `88fa8ae`, while the separately running
processor checkout remains `d1f4317`. The current fixes were not installed there.
The earlier release workflow timed out at readiness even though subsequent
public checks returned the correct API version and database readiness. This is
not treated as an ongoing outage.

The user's one-visible-recording report is **not closed**: protected production
inventory remains unverified. The browser permission gate declined observation
of the existing live page; no alternate credential access was used. Source
inventory, session grouping and local proposals do not prove every recording is
uploaded or complete in production.

Remaining work includes the specialist and conditioned-product rows in
`spec-coverage.md`, actual reviewed training/calibration/held-out populations,
task-specific fitting and accuracy gates, sustained full-game/performance work,
staff task acceptance and a coordinated compatible production journey. Passing
protocol tests does not satisfy those obligations.
