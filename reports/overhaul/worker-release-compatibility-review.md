# Processor/API release compatibility review — 2026-10-21

This review is read-only. No worker, scheduled task, credentials, service,
deployment or production database was changed. The new continuation is based
on `549e8cf30ced3f4ed6f914f2a21263a0baf226d3`; its subsequent fixes remain local.

## Observed identities

At 21:23:46 UTC the canonical Vercel frontend served production deployment
`dpl_EkjHxmNNKzQDkw6hjLJt9CauKHxu`, source `549e8cf`. Public API health and
database readiness returned 200, but `/api/v1/system/version` still returned
`7bb3793cd9be08e44a609510c093612a8fd7c02a`. The protected release inventory
returned 401 and was not accessed with staff credentials. Evidence:
`runs/readonly-cross-release-549e8cf.json`.

At 21:35:08 UTC the reviewer independently confirmed that GitHub CI run
[39676607970](https://github.com/aahmadf123/UT-Football-IQ/actions/runs/39676607970)
for `549e8cf` completed with two failures: worker unit tests and cross-service
integration. Seven other jobs succeeded. Safe metadata is retained in
`runs/readonly-ci-549e8cf.json`. The local hard-link/ctime source-identity fix
has passing Windows, Linux-container and transport evidence; that does not
change the recorded remote CI result.

The reviewer refreshed sanitized process metadata: PID `28716` remains a Python
process created at 10:24:06 UTC. Its previously verified application entrypoint
belongs to `C:\Users\firas\footiq-worker\gpu-worker`; that separate checkout is
still at `d1f4317bae5529864bd2d9df80713515dd5d9891`. The interpreter path is not
proof of imported application modules. No environment or full command line was
printed. The current development checkout is a different source tree.

## Concrete compatibility defect

Before the new capability correction, `backend/app/routers/jobs.py` filters
claims by advertised job types and processing profiles. It excludes the expected
outcome verification variant for an untrusted processor principal, but has no
training-variant/protocol compatibility requirement. The old `d1f4317` worker
advertises `train`; its dispatch explicitly accepts only `local_coverage` and
raises for other training variants. It can therefore consume and fail a new
`expected_outcomes` job, or a verification job when its principal is trusted.
An API deployment alone does not update that processor.

This is not a claim that every legacy pipeline request fails authorization.
`bind_media_identity_completion` explicitly permits historical jobs without a
source receipt to complete, without granting a trusted source proof. Legacy
processing can consequently report completion while lacking current source,
feature-export and classification proofs, native-media behavior and specialist
products. Existing publication gates must continue to withhold unsupported
facts. Healthy heartbeat, successful claim and terminal job status alone do not
prove compatibility.

The reviewed local correction adds producer protocol 1 plus explicit supported
training variants so unsupported new jobs stay queued. It covers claim-body
metadata and registered-runtime fallback, rejects a boolean protocol, and
retains ordinary-principal exclusion from the verifier. The reviewer read the
actual database regressions and independently passed 21 worker capability/
dispatch cases in 1.07s. This is a local source closure, not a production update.
Principal ownership, live lease, source proofs and independent verifier checks remain separate
authorization boundaries. Startup source/dirty metadata is useful inventory;
it cannot prove every module imported later. Unknown Git inventory does not
prevent basic CPU processing. Legacy pipelines and local-coverage tasks retain
their existing claim policy; those workers still require the coordinated update
below to supply the current products and proofs.

A fresh read-only check at 21:48:20 UTC still found the same canonical frontend
deployment/`549e8cf` and backend `7bb3793`. Ordinary httpx requests with the
frontend Origin returned 200 for edge health, backend health, database readiness
and version; the protected inventory returned 401. Evidence is retained in
`runs/readonly-cross-release-549e8cf-final.json`. The earlier urllib probe in
that same record received Cloudflare error 1010 (`browser_signature_banned`);
this client-specific denial is not reported as a backend outage. No staff
credentials were used for either attempt.

## Coordinated release handoff

These are future operator steps, not actions performed by this review.

1. Finish and review a committed candidate before releasing it to main. Pass its
   branch/local checks, including the Linux source-identity case and all three
   actual transports. Main CI automatically triggers API deployment, so do not
   place the processing pause after that trigger. Inspect active/queued jobs through an authorized staff session;
   the reviewer has not established that the production queue is idle.
2. Stage an immutable clean worker checkout at that SHA outside the running
   checkout. Verify dependencies, cached actual model hashes, FFmpeg/ffprobe,
   GPU support and the exact worker interpreter. Preserve private configuration,
   DPAPI secret, source films, cached weights and prior result/checkpoint data.
   Do not update files under a running interpreter.
3. Before the main push/merge that can trigger deployment, schedule a bounded
   processing handoff. Drain or deliberately pause the
   identified worker's active job and confirm the result/lease state. Prevent
   its owned runner/watchdog from relaunching the old code, then stop only the
   verified owned runner/child tree. Do not stop all Python processes or delete
   queued/history rows. A scheduled-task stop alone can leave a child alive.
4. Release the reviewed candidate to main, wait for its successful CI/deployment,
   and confirm the exact backend/edge serving SHA and
   database readiness. Keep processing stopped during the transition: the new
   worker uses new batch/actor/source endpoints that the old API need not have.
   Vercel is independent; confirm its canonical frontend SHA as well.
5. Confirm the staged clean checkout is at the actual final release SHA (account
   for a merge commit before starting it). Point the existing private worker
   configuration at that checkout and verified interpreter, preserving its
   credential/model settings, then start
   the owned runner. Check registration owner, actual profiles/models, producer
   protocol, supported variants, source SHA/dirty state and fresh heartbeat.
   An existing worker ID registered to another user does not become a trusted
   processor merely by a registration upsert.
6. Run a bounded authorized real-film staff upload through that exact frontend,
   API and worker. Verify the actual claim/attempt, early private full-source
   proof, completion receipt, clean signed media, native mapping, correction and
   playback. Unknown identity/calibration/model output must remain unavailable.
   Exercise actual candidate training/verifier transport only with appropriate
   evidence; numerical protocol fixtures do not qualify football models.
7. Preserve previous checkout/configuration references for a deliberate rollback.
   A mixed frontend/API/processor release or heartbeat-only success is not final
   acceptance. Full-game, staff benchmark, specialist-model and performance
   obligations remain open.

The existing `install-windows-worker.ps1` is an installer that rewrites private
configuration and force-stops matching runner trees. It is not a safe blind
upgrade command for this handoff. The current runner reads its configuration
once and has restart/watchdog behavior, so exact owned-process handling matters.
