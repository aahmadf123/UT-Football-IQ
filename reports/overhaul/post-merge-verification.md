# PR 57 post-merge verification

Observed 2026-10-21, through 16:35 UTC (12:35 PM America/New_York).
This is a point-in-time audit, not a release-readiness certificate.

## Verified

- PR 57 was merged into `main` as `7bb3793cd9be08e44a609510c093612a8fd7c02a`.
- All nine jobs in [CI run 39634855034](https://github.com/aahmadf123/UT-Football-IQ/actions/runs/39634855034) passed.
- [Deploy run 39634854920](https://github.com/aahmadf123/UT-Football-IQ/actions/runs/39634854920) completed successfully at 16:32:05 UTC. This proves the deployment commands succeeded, not that the application is ready.
- The edge Worker is version `f60a365f-49f6-4e9e-ab3b-d49475a05354`, with the merge SHA. Edge health returned 200 and canonical frontend CORS preflight returned 204.
- The deployment log identifies the new backend image as `sha256:ffd45bca5cb1e8ab0326c579ab9443dd66d49307ae0f1056cacb43518f93c65e`.
- An independent reviewer verified container target version 41 had 100% allocation at 16:32:28 UTC. The application still had an active rollout and reported scheduling=1, active=0, healthy=0. Allocation alone does not establish successful startup.

## Production remains unverified

- Public backend version checks at 16:29 and 16:32 UTC returned the previous SHA `d1f4317bae5529864bd2d9df80713515dd5d9891`; the new release-inventory route returned 404.
- At 16:34:07 UTC, `/health`, `/ready`, `/api/v1/system/version`, metrics definitions and release inventory all timed out with a 20-second client read timeout. The edge itself and frontend still responded. See `post-merge-public-health.json` for the most recent probe.
- Canonical Vercel production still resolves to deployment `dpl_3Qo46yh1or7sPHrhZPtWvpKm87tM`, SHA `d1f4317bae5529864bd2d9df80713515dd5d9891`. A query filtered to the merge SHA returned zero deployments. The source-branch preview for `ca64e0a` is ready; it is not the canonical production deployment.
- [Live smoke run 39635632655](https://github.com/aahmadf123/UT-Football-IQ/actions/runs/39635632655) completed with failure. Both tests failed after retries: the expected-release request timed out, and the page crawl encountered a restarting-server message / roster request timeout. These errors do not establish incorrect login secrets. A successful command-only deployment does not replace this check.
- Independent Cloudflare log review found `TooManyConnectionsError` at 16:26:03 and 16:27:12 UTC. Staff-worker heartbeats had returned 200 through 16:25:00, then 500 at 16:26:15, followed by canceled attempts. These errors predate the new edge deployment; they are not proof of a PR 57 regression.
- Current schema head, successful staff-worker heartbeat, worker capabilities/model inventory, and an upload-to-playback journey have not been verified in production.
- The independent review refresh at 16:34:34 UTC found starting=1, active=0, healthy=0. Continued database connection errors at 16:34:22 and 16:34:29 had the same deployment/placement identifiers as the pre-rollout errors. New-image startup or migration failure has not been proven; successful startup has also not been proven.

## Recovery and release checks

1. Correlate the new container's placement/startup/migration logs with origin database connection capacity. Do not guess capacity or run migrations against an unverified database URL.
2. Establish that the backend serves the merge SHA and that `/ready` succeeds. Startup already runs Alembic before serving; a manual migration is not automatically required.
3. Resolve the absent Vercel main deployment and verify the canonical frontend build identity.
4. Require authenticated schema/worker inventory and the live page crawl, then a bounded authorized upload, processing, review, overlay playback and export check.

The existing deploy workflow runs independently of CI and prunes older image tags after deployment without a version/readiness gate. Its prune step ran in this deployment. A prior image may require rebuilding for rollback; no rollback was performed.

No production deployment, restart, configuration change, database query, login, upload or other write was initiated by this audit. The already-configured GitHub deployment and smoke workflows ran after the user's merge. Local audit reports were the only files added.
