# ADR 0007: Shared hosted film review and CPU processing

Status: accepted implementation of the requested second revamp. Extends ADR 0006
with shared hosted operation; independent local installations remain supported.

## Decision

Use the existing Vercel frontend, Cloudflare Worker/Container and R2, and the
existing PostgreSQL database. Store media in R2 and sessions, recording metadata,
jobs, corrections and model evidence in PostgreSQL. Authorized accounts connected
to this API see the same library. No additional compute provider is required.

The Cloudflare Container supervises one API process and one CPU pipeline process,
with separate Python environments. The API never imports inference libraries.
A dedicated existing staff service account authenticates the pipeline. Startup
runs schema migrations before serving, verifies detector readiness, and recovers
only this hosted worker's shutdown-interrupted jobs. Coach pauses remain paused.
Active leased work keeps the container awake; ordinary idle polling does not.

The image installs the existing YOLOv8n COCO person baseline from its pinned URL
and verifies its checksum. Runtime readiness requires a real CPU inference check.
Missing or unusable weights leave model-free Review available. The hosted worker
checks temporary disk and source limits before expensive processing; see the
current limits in `docs/setup/cloudflare.md`. Full recordings remain stored and
reviewable even when they exceed the hosted processing budget. No cloud tier is
automatically increased.

## Film and review contracts

- Verify finalized object bytes on the server, persist SHA-256/byte size, and
  enforce unique verified content under a database constraint and transaction
  lock. A duplicate returns the existing recording without changing its session,
  owner, tags or camera. The UI identifies reuse and links to that recording.
- Existing records with no fingerprint remain unchanged until re-registration.
  This migration does not merge historical records or delete duplicate media
  objects; those may still have existing references.
- Show tracking beside playing film, using decoded video frame time. Convert
  recording and clip clocks explicitly. Track boxes retain original source
  coordinates when playback uses a resized H264 proxy. Unsupported originals
  fall back to browser-compatible processed clips.
- Preserve independent recording identities and camera calibration. A recording
  can have a play queue, selected-track inspector, layers, trails and an event
  timeline without claiming automatic identity across cameras.
- Read-only accounts cannot upload or start processing; the API enforces this
  independently of the UI.

## Learning and external game context

Pretrained player detections are candidate observations. They do not establish
jersey identities, football events, calibrated field measurements or tactical
accuracy. Actual staff corrections, disjoint session splits, held-out evaluation
and administrator model promotion continue to govern the existing coverage
learning workflow. Saving labels does not silently retrain a detector. This
revamp does not add an unvalidated zero-shot or autonomous detector-training claim.

CFBD credentials are forwarded privately to the API container. Ordinary data
reads use the cache. Administrators can refresh bounded season scopes or one
week of plays, with serialized refreshes and safe failure summaries. Previously
cached data survives upstream failures. Schedule entries open cached play-by-play;
external game context is labeled separately from observations derived from film.

## Validation

Use the full frontend checks and affected backend/worker/edge suites, migration
round-trips, and real local Chromium workflows. Browser acceptance includes
upload, second-account visibility, viewer permission denials, concurrent duplicate
reuse, model-free Review, pretrained CPU processing, video playback and synchronized
tracking. Synthetic wiring fixtures are identified separately from the repository's
actual drone footage. These checks establish workflow correctness, not Hudl or
SkillCorner accuracy. Hosted deployment and provider API access require current
connections and allowed network destinations.
