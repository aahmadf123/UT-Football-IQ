# Staff labeling package

Start with `reports/overhaul/labeling-package/sources.json` and the local
`tools/football-labeler/index.html` viewer. The package contains source metadata,
empty drafts and reviewer assignments. It contains no footage, completed labels,
assigned staff identities, confirmed source groups or claimed benchmark results.

Use the [local viewer launch and frame-index instructions](../../tools/football-labeler/README.md)
to serve it on this computer; select the original films through its file picker.

The current register has 36 recordings: thirty practice extracts and six game
files. The April 16 practice filenames propose **one session**, not thirty
independent groups. Each game file has a separate proposed group, pending staff
verification of the actual game and related views. No television source category
has been confirmed. There are seven proposed groups and zero confirmed groups;
even if all seven prove independent, at least five more groups are needed for
the twelve-group held-out benchmark. Category balance may require more sources.

## Files to use

| File | Purpose |
| --- | --- |
| `sources.json` | Stable source IDs, local paths, exact probed sizes/durations/dimensions, rational frame rates, proposed group/category and pending hashes. |
| `source-register.csv` | Spreadsheet version of the register. Edits to this CSV do not automatically change the JSON catalog. |
| `review-assignments.csv` | Unassigned primary, independent secondary and adjudication passes. Fill in actual staff IDs and track progress. |
| `drafts/source-….json` | One empty primary draft per recording. Save each reviewer/pass as a separate working file. |
| `dense-coverage-draft.json` | Unreviewed scaffold for actual assigned-frame coverage; every attestation starts false. |
| `readiness.json` | Targets and current evidence gaps. |
| `split-plan.json` | Empty training/calibration/holdout split plan. Confirm groups before assigning any split. |

Open the viewer, load the catalog and choose the original local film file. Confirm
the category and independent group after reviewing its content and game/session
records. Export the confirmed catalog as `sources-confirmed.json`. Fill in an
actual reviewer ID, annotate in original-source seconds, and save a draft.
Returning to a different reviewer/pass must start from its own saved draft or a
blank pass. Secondary staff should observe independently before comparing with
primary labels. Use the [codebook](codebook.md) for unknowns, camera occurrences,
track visibility and identity evidence.

Unknown-snap, aborted, false-start and other non-countable sequences stay in the
working draft. Freezing retains them separately under `sequences`, with
`canonical_count_eligible: false` and the staff reason or `no_observed_snap`.
A missing snap does not imply a false start. Only eligible finite-snap `plays`
contribute to the normal-play benchmark. An all-sequence source can be preserved
but contributes zero eligible normal plays; no observation is silently dropped.

## Required population

| Requirement | Minimum |
| --- | --- |
| Distinct held-out canonical plays | 240 |
| Independent held-out session/game groups | 12 |
| Each of practice, coach game, television | 80 plays from 4 independent groups |
| Densely annotated tracking plays | 48 total; 16 in each category |
| Independent double labels with adjudication | At least 20% of canonical plays; at least 48 when using 240 |
| Automatic identity evaluation | Target 2,000 accepted canonical athlete cases; at least 99.5% precision, two-sided 95% Wilson lower bound at least 99%, and at least 90% readable/team-resolved coverage |

These targets describe **held-out** evaluation. Additional independent sources are
needed for training and calibration. Keep every clip, replay and alternate view
from one session/game in the same split. More windows do not create more plays,
games or independent identity cases. The current six game files cannot be
assumed to supply both coach-game and television categories.

## Hash the original bytes

Hashing runs locally in 1 MiB chunks, including multi-gigabyte files. The viewer
does not need to read an entire game into a browser buffer. Source byte-size or
modification changes during hashing cause refusal. Originals are read only.

```powershell
python scripts/prepare_overhaul_labeling.py hash --catalog runs/labeling/sources-confirmed.json --output runs/labeling/sources-hashed.json
```

Use `--source-id source-…` one or more times to hash only selected recordings.
Otherwise every catalog source is hashed. All outputs must be new paths; the
tool refuses to overwrite catalogs, drafts, originals or earlier frozen files.
Use a new filename for each revision. If media moves to another staff computer,
update its catalog `path`; source IDs remain stable for the same library/name.
Recheck any size/hash mismatch before continuing.

The helper was also exercised on the actual 8.67 MB practice source and 3.74 GB
full-game source in 3.751 s total, using 1 MiB reads; the small evidence record is
`reports/overhaul/labeling-hash-canary.json`. This verifies streaming execution,
not annotation quality. The delivered base catalog remains hash pending.

## Freeze a completed primary review

Only run this after the named staff member has actually reviewed the annotations
against the exact source identified in the confirmed catalog. A pending draft
hash is permitted because this command binds the reviewed source bytes locally;
if a prior hash exists it must match. Do not attest to a different same-name film.

```powershell
python scripts/prepare_overhaul_labeling.py freeze --catalog runs/labeling/sources-hashed.json --draft runs/labeling/primary.json --attest-reviewed --output runs/labeling/frozen-primary.json --entry-output runs/labeling/source-entry.json
```

The command checks source context, observed boundaries, visible boxes, target
links and explicit reviewer identity. It writes `evidence_kind: human_reviewed`
only with that explicit review attestation. Primary-only labels retain
`double_labeled: false` and `adjudicated: false`. Checkboxes in one draft do not
establish independent review. The command validates structure and byte lineage;
it cannot prove that someone watched the footage or that annotations are correct.

## Freeze an independently double-reviewed subset

Save separate primary, secondary and adjudication drafts, with distinct primary
and secondary reviewer IDs. After independent observations are complete, align
source-local window IDs and canonical IDs without discarding disagreements.
Adjudication records the resolved observations for every selected occurrence.
List each selected canonical play explicitly; repeat `--double-play` as needed.

```powershell
python scripts/prepare_overhaul_labeling.py freeze --catalog runs/labeling/sources-hashed.json --draft runs/labeling/primary.json --secondary runs/labeling/secondary.json --adjudication runs/labeling/adjudication.json --double-play session-play-001 --attest-reviewed --attest-adjudicated --output runs/labeling/frozen-adjudicated.json --entry-output runs/labeling/adjudicated-source-entry.json
```

Every source-local occurrence of the selected canonical play must be present in
all three passes. The freezer uses the adjudicated observations for that subset;
other windows retain primary observations and single-review status. A missing
angle/replay occurrence, reused primary/secondary person, inconsistent source,
invalid box or absent attestation causes refusal. Keep the original pass drafts
for audit; frozen labels do not replace them.

## Assemble evidence without claiming a release

The generated `source-entry.json` contains the actual media and frozen-label
SHA-256 references in the schema expected by
[`docs/overhaul-release-evidence.md`](../overhaul-release-evidence.md). Copy the
entry into a **new** release manifest's `sources` list, assign confirmed group
IDs to disjoint splits, and attach actual evaluated predictions, model hashes
and evaluation reports. Keep reviewed staff exports in the local library or
ignored `runs/labeling/`; do not commit private media or filled player labels.

```powershell
python scripts/evaluate_overhaul_release.py runs/labeling/release-evidence.json --output runs/labeling/release-gates.json
```

Preparing or freezing labels does not run the evaluation, train/promote a model,
qualify tracking/identity, or satisfy the release gates. The generated package
is deliberately unreviewed and remains outside production analytics.

To regenerate untouched templates, use a fresh directory:

```powershell
python scripts/prepare_overhaul_labeling.py prepare --output runs/labeling/new-package
python -m unittest discover -s tests_local -p test_overhaul_labeling.py -v
```
