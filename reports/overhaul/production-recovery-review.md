# Production recovery review — 2026-10-21

Independent read-only review. No service restart, deployment, configuration
change, database connection/query, credential reuse or production write was
performed. Cloudflare telemetry queries used `dry: true`; secret values were
neither requested as fields nor displayed.

## Current observed state

At **16:48:22 UTC**, public probes returned edge health 200 (0.28 s), backend
health 200 (0.31 s, uptime 805.7 s), `/ready` 200 (0.45 s, database `ok`) and
`/api/v1/system/version` 200 (0.30 s) with merge SHA
`7bb3793cd9be08e44a609510c093612a8fd7c02a`.

Cloudflare application `a034d609-53a2-49dc-83a2-55ce19d45a80` reports version
**41**, updated **16:34:57.957 UTC**, image
`sha256:ffd45bca5cb1e8ab0326c579ab9443dd66d49307ae0f1056cacb43518f93c65e`.
The primary instance is running in `ord15`, placement started **16:32:16.404**,
status updated **16:34:50.298**. Aggregate active count is one; its separate
`healthy` counter remains zero and does not override the actual successful API
and database probes. Version 41 has 100% allocation; version 40 has zero.

Staff-worker heartbeat requests for `laptop-ahmad_legion` returned 200 through
**16:48:39.010 UTC** (all twelve latest sampled heartbeat events succeeded).
This confirms current heartbeat connectivity, not worker source/model versions
or end-to-end inference quality. Hosted container processing remains disabled
by the deployed `HOSTED_PROCESSING_ENABLED=0` setting.
The fresh dry telemetry window **16:45–16:53:08 UTC** contained zero matching
`TooManyConnectionsError` events. This is a bounded observation, not a capacity
or load-test result.

Vercel now has automatic Git-sourced **READY production** deployment
`dpl_9D6tjefcin94zecnYMLSTTREYj4S` for the merge SHA. It was created
**16:37:06.585**, began building **16:37:08.019**, and became ready
**16:38:04.045 UTC**. Its aliases include `ut-football-iq.vercel.app` and the
main-branch alias. GitHub's Vercel commit status is success at **16:38:04 UTC**.
The previously absent deployment has appeared without manual intervention;
the observed metadata does not establish why creation was delayed.

## Failure evidence and its limits

- Before the new edge deployment at **16:27:32.830**, the old serving placement
  logged `TooManyConnectionsError`, including failed worker heartbeats at
  **16:26:03** and **16:27:12**. These cannot be classified as PR57 regressions.
- Between **16:32 and 16:34**, the same error continued on old deployment
  `d031ae4e-2962-4268-9312-55d5beb58c71`, placement
  `fe8e28ac-9ee5-e4d2-3fe7-4a98e79f41e9`. Target-image allocation and successful
  Wrangler completion did not yet prove the actual API had switched.
- The replacement placement also logged this error, last observed in the
  bounded sample at **16:41:27.678 UTC** on `/api/v1/players`, deployment
  `d035ea9d-0ae5-4c7e-9dc3-c9d77b50a93b`, placement
  `dc94fec0-2560-f89f-9075-0601e40d71d3`. Current readiness and heartbeats later
  recovered. The error identifies database connection exhaustion; it does not
  identify which clients consumed capacity or prove a connection leak.
- No `hosted_schema_migration`, API-start-failed or API-exited event was found
  in the requested startup-log windows. Missing telemetry is not migration
  failure evidence. The actual new API build is now confirmed publicly.

## Database and schema evidence

Safe Hyperdrive metadata identifies origin host
`aws-us-east-2-2.pg.psdb.cloud`, port 5432, with query caching disabled. This
hostname identifies the configured Hyperdrive origin as PlanetScale Postgres.
The container uses the separate `BACKEND_DATABASE_URL` secret; its value was
not read, so identical container-origin configuration is **not verified**.
No connected PlanetScale control-plane tool is available. The actual database
plan, `max_connections`, role limits and client attribution remain unknown.

The checked-in backend pool permits ten retained plus twenty overflow
connections per API process (`backend/app/database.py`). This is an identifiable
connection-pressure risk, especially during overlapping placements; it does not
establish the provider's actual connection cap. PlanetScale recommends bounded
application pooling and provides PgBouncer on a distinct port; changing a
connection endpoint requires separate compatibility verification and was not
performed. [PlanetScale connection guidance](https://planetscale.com/blog/scaling-postgres-connections-with-pgbouncer).

CI run `39634855034`, backend job `119342483902`, directly records upgrades
through `0010_clip_edits` at **16:28:00.992 UTC**, followed by downgrade to base
and a second complete upgrade at **16:29:15.810**. This proves the merge's CI
migration round trip, not the production database's current revision. Production
revision still requires authenticated release-inventory evidence; no staff
session or credential was reused.

## Review direction

No emergency restart or manual deployment is supported by the currently healthy
sample. Actual provider capacity has not been established, so no pool-default
or endpoint change is recommended from this evidence alone. Review the proposed
readiness/build-version deployment gate, transient-rollout retry, stale-commit
guard and retained distinct rollback images. A green deploy command alone must
not declare an application healthy. Validate those changes in tests before any
later rollout. Full football-model and platform acceptance remains separate
from this infrastructure recovery.

## Release-hardening code review

Reviewed the follow-up deployment/live-smoke workflows, readiness/retention
helper and frontend release probe. These changes are local and were not
deployed during this review.

- Deployment waits for successful trusted-repository main-push CI and checks
  current main immediately before Wrangler, preventing out-of-order completed
  CI runs from deploying an obsolete commit.
- The helper requires the exact expected serving SHA plus database readiness
  for three consecutive checks. Timeouts, unavailable responses and intervening
  old builds reset the count; no successful record is written on timeout.
- Pruning follows readiness and uses successful CI artifact history. Bootstrap
  and missing protected tags preserve all images. History retains three distinct
  image digests, rather than treating aliases as different rollback generations.
- Review reproduced a malformed-history deletion defect: two digests sharing
  one tag could falsely satisfy the three-image threshold. The corrected helper
  rejects conflicting tag/digest mappings and requires three distinct protected
  tags. All **nine** focused Python regressions passed independently.
- Actual previous deployment logs confirm the currently used eight-hex-digit
  tags and digest format. The current image log digest exactly matches the
  observed Cloudflare version-41 digest.
- The frontend release probe treats transient request/JSON failures as retryable
  observations. Both initial and steady polling phases continue to require the
  expected SHA. The owner reported two focused frontend regressions and type
  checking passed, plus a read-only three-check production readiness run.

No additional blocking correctness defect was found in this reviewed scope.
Actual provider capacity, production schema revision, staff model/source
versions and football accuracy remain separate unverified items.

## Processor source origin — 18:08–18:10 UTC

Read-only Windows process metadata distinguishes the long-lived processor from
the local verification worker. No process was stopped or restarted; no
environment variables, credential values or full command lines were emitted.

Serving identities remain separate: the earlier source-bound frontend and API
checks observed merged release `7bb3793cd9be08e44a609510c093612a8fd7c02a`;
the processor entrypoint below belongs to `d1f4317`. Neither identifies this
local, uncommitted continuation as deployed. Updating the processor and proving
one compatible frontend/API/worker release belong to final end-to-end acceptance.

- PID 28716 started at 10:24:06 UTC. Its working directory and whitelisted Python
  entrypoint are `C:\Users\firas\footiq-worker\gpu-worker` and that directory's
  `__main__.py`. It has an established outbound TLS connection on port 443 and a
  worker port-9090 listener. The parent interpreter is the `.venv-gpu` executable
  in the current development checkout; sharing an interpreter does not establish
  shared application source.
- The separate `C:\Users\firas\footiq-worker` directory is not a junction. Its
  Git working tree is clean at `d1f4317bae5529864bd2d9df80713515dd5d9891`.
  The on-disk entrypoint SHA-256 is
  `aa59173cdf12f2c4b028c2e5924ee8141d9055c0e018800b4d7b1b7f114e2c56`;
  its orchestrator SHA-256 is
  `3524b43839f4d7feddbf596da3d4cae2eefe641286dd9548e2bb6a7e3acc469b`.
  Both differ from the currently edited development files.
- Local worker PID 19268 started at 15:25:49 UTC, has the development checkout as
  its working directory, holds `runs/overhaul-flow/logs/worker.log`, and has an
  established loopback connection to API port 8011. Its API process holds the
  local flow log and connects to the disposable PostgreSQL port 55432.

The observed long-lived entrypoint therefore belongs to the old clean worker
checkout. There is no evidence that current uncommitted specialist modules are
deployed. Process metadata alone does not expose exact already-imported module
bytes or prove which remote API hostname a TLS connection serves; those details
remain unverified rather than inferred from healthy provider heartbeats.

## Later read-only observation — 19:42:49 UTC

The parent reported a fresh read-only public readiness run with three
consecutive exact `7bb3793cd9be08e44a609510c093612a8fd7c02a` plus database-readiness
checks passing at 19:42:49 UTC. This is the merged API serving identity, not the
local continuation. The separately inspected processor entrypoint remains the
old `d1f4317` checkout; no processor update/restart or production deployment
occurred during the continuation. Public readiness does not establish
authenticated staff upload/result acceptance, current production schema or
qualified model inventory.
