# Post-push continuation verification — 2026-10-21

The local continuation after `549e8cf` passes the checks below. **The full twenty-page overhaul is incomplete.** These latest changes are uncommitted and undeployed. No production worker, credentials, deployment or production data was changed by this continuation.

## Implemented and independently reviewed

- Fixed the Linux CI source-identity failure: POSIX hard-link metadata changes trigger a full content recheck instead of rejecting unchanged film. Identity/size/mtime mismatches fail closed, and the ctime-only recheck rejects changed bytes; unchanged metadata avoids another full read.
- Added paired trench frame replay using explicit interaction endpoints, safe calibration and actual native source samples. Missing, predicted or interpolated actors stay absent; traces stop at gaps. Conflicting actor aliases and multiple interaction starts abstain. Temporal duration remains available without field geometry.
- Added RB measured-sample density, explicit contact marks, original named gaps and source-film actions. A true partial carrier-path composite supports visible prefixes without inventing contact, LOS entry or terminal gain. Native clocks, conflicting possession/contact evidence and outermost tackle/TE edge geometry are checked.
- Added frozen held-out source cases and category diagnostics for all four expected-outcome heads: authenticated filtering, pagination, baseline/calibration/error comparisons, selected-case downloads and exact current film links. Back restores report/model/cohort/category/page, including untouched All cases/page zero. Descriptive subsets do not inherit release approval. Generic classifier cases remain unavailable.
- Made outcome report/evidence storage content addressed and candidate identity bind weights, report and evidence hashes. Identical weights with changed reports preserve historical bytes; re-registering an identical artifact binding is idempotent.
- Fixed failed-after-fit training Retry by isolating queued output directories by job and fenced attempt. Actual fit/registry-failure regressions verify both new-job Retry and reclaimed-attempt recovery preserve earlier bytes; same-attempt replay and direct CLI still refuse overwrites.
- Added worker protocol/task advertisement and claim compatibility so older processors cannot consume unsupported new training jobs. Principal, runtime, lease and independent-verifier boundaries remain separate. Startup checkout/dirty metadata is inventory evidence rather than proof of every later import.

## Final local verification

| Check | Result |
| --- | --- |
| Backend | 1,046 passed; 9 deprecation/coroutine warnings |
| Worker, Windows | 1,810 passed; 1 optional joblib artifact test skipped; 3 integrations run separately |
| Worker, clean Linux | 1,810 passed; same skip/deselection; Python 3.12.15, FFmpeg 7.1.5, CI requirements |
| Frontend unit | 566 passed across 79 files |
| Browser | All 28 flows passed, using explicitly mocked browser APIs |
| Actual local HTTP integrations | All 3 passed after the final queued-Retry correction: normal pipeline, candidate/verifier and generic-label producer |
| Frontend production | Global ESLint and build passed; 17 static route outputs |
| Static checks | Backend Ruff/format (236 files)/mypy (123 app files), worker global Ruff and git diff check passed |

The source manifest covers 48 changed implementation/test/fixture files against base `549e8cf`, with aggregate SHA-256 `ccda8e1a5f008c450a0002881316bc2ecba8308d17e2b0f5d8d06f344f71e6a1`. The [machine record](post-push-verification-20261021.json) includes every file and seven final log hashes. The clean Linux snapshot at `runs/post-push-ci-source/source-manifest.json` contains 487 copied source/fixture files matching current bytes; it excludes environments, weights and films. An initial broader container attempt at `runs/post-push-linux-source` used an incomplete read-only source mount and old FFmpeg; its setup failures were superseded by the clean CI-style container run. No original film was altered.

These are regression and protocol results. The browser uses mock APIs; the three HTTP transports use actual disposable local services and explicitly synthetic protocol evidence. They establish neither a real football model's accuracy nor a deployed staff acceptance trace. Earlier real 720p canaries remain historical evidence from before this latest continuation.

## Pushed and deployed state

The user's `549e8cf` is on remote `main`. [CI run 39676607970](https://github.com/aahmadf123/UT-Football-IQ/actions/runs/39676607970) failed its worker unit and pipeline integration checks; the deployment workflow was skipped. These uncommitted fixes have not changed that remote result.

The independent read-only observation at 21:48:20 UTC found canonical Vercel frontend `549e8cf`, backend `7bb3793`, and public health/readiness/version responding with database okay. Protected inventory returned 401 without staff credentials. The separately inspected running worker checkout remains `d1f4317`; it was not updated or restarted. This is a mixed release, not verified production compatibility.

The [coordinated worker/API handoff](worker-release-compatibility-review.md) requires preparing the exact committed candidate and draining/pausing the owned old processor before a main push/merge can trigger automatic API deployment. Start the matching immutable worker only after the API/frontend serving revisions are confirmed, then perform a real staff upload-to-playback/correction trace. A normal push alone does not update the GPU processor.

## Still required

The [page-by-page ledger](spec-coverage.md) retains the remaining implementation: conditioned coaching comparisons, generic classifier cases, further route/trench/defense/special-teams products and trained specialist coverage. Actual staff benchmark labels, qualified model/identity/tracking evaluations, whole-original-game processing, sustained workload/performance, staff usability acceptance and a compatible authenticated production journey remain unmet. The user accepted available 720p engineering footage; the original native-1080p performance gate remains deferred.

The staff labeling package remains prepared with unreviewed templates; no staff labels or approvals were invented. Passing numerical fixtures cannot close these gates.
