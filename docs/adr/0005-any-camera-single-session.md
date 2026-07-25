# ADR 0005 — Any-camera capture: `unconstrained` is a first-class regime

- **Status:** Accepted
- **Date:** 2026-07-24
- **Supersedes:** the "single **overhead** camera" language of ADR 0001 §6 and
  the mandatory capture standards of `docs/capture-protocol-v1.md` (v1)
- **Preserves:** ADR 0001's one-camera-per-session rule and its render-layer
  vocabulary (`raw | tracks | labels | events | metrics | wireframe`)

## Context

ADR 0001 fixed two things under one heading: (a) Football-IQ analyzes **one
video stream per session** (no Hudl-style endzone+sideline multi-angle sync),
and (b) that stream is an **overhead drone capture** meeting the v1 capture
protocol (90–120 ft AGL, 4K/60, `TOL_*_DRONEA.mp4` naming).

Decision (a) has held up. Decision (b) has not:

- The owner's product goal is explicit: accept footage **from anyone, shot
  any way** — any angle, height, camera, or filename — with zero coordination
  with the videographer. Requiring capture standards is the Hudl-style
  operational burden this product exists to remove.
- Reality already violated v1: the actual film in production is named
  `Dji 20260416110000 0257 D.mp4` (720p/30), not the mandated scheme, and the
  pixel-only regime detector (Issue #126) classified real drone footage as
  `unknown` — which the pipeline then silently treated as `drone_follow`.
- The regime vocabulary had no honest place for "perfectly good footage that
  is neither drone-follow nor press-box": endzone towers, handheld phones,
  tripods. They collapsed into `unknown`, an error-flavored label that also
  absorbed genuine failures.

## Decision

1. **`unconstrained` becomes a first-class capture regime.** Analyzed footage
   that matches neither special regime is `unconstrained` — a supported mode,
   not a fallback. `unknown` narrows to hard analysis failures (unreadable
   file, no frames).
2. **`unconstrained` runs the generic pipeline path with recall-first
   detection** (SAHI tiling + dual-resolution, same as `drone_follow`),
   because arbitrary viewpoints may render players small. `fixed_sideline`
   keeps its cheaper full-frame pass.
3. **Calibration is opportunistic on every regime; suppression is always
   explained.** When field lines aren't visible enough to calibrate,
   spatial metrics are suppressed exactly as before (`analytics_safe=False`)
   — but the clip-overlay payload now carries a coach-readable reason
   ("Field yard lines aren't visible enough from this camera angle …")
   sourced from the calibration's stored `reason_codes`. Silence is not an
   acceptable failure mode for a coach-facing product.
4. **The capture protocol becomes a guide, not a gate.** `capture-protocol-v1.md`
   is rewritten as "better footage → more metrics" guidance. No capture
   property is validated as a requirement; ingest warnings (low resolution /
   fps / codec) stay warnings.
5. **One camera per session stands.** Multi-angle synchronized analysis
   remains out of scope exactly as ADR 0001 decided; a future change still
   requires a new ADR. Render layers on the single stream are unchanged.

## Consequences

- `capture_regime` enum (DB + models + regime detector) gains
  `unconstrained` (migration 0031). Existing `unknown` rows keep their value
  — they predate the split and are treated as generic footage.
- `GET /api/v1/clips/{id}/overlays` gains `capture_regime` and a
  `calibration {analytics_safe, reason, reason_codes, confidence}` block for
  the clip-review banner and gated analytics cards.
- Detection composition keys on a small-player regime set
  (`drone_follow`, `unconstrained`) instead of drone alone. The
  drone-distilled student model remains gated to `drone_follow` only.
- The v1 capture standards live on solely as optional drone-pilot tips.

## Alternatives considered

- **Treat `unknown` as the any-camera label.** Rejected: it conflates "we
  couldn't read your file" with "your camera angle is fine, just generic",
  which poisons both telemetry and coach-facing messaging.
- **Add per-viewpoint regimes (endzone, handheld, …).** Rejected for now:
  no evidence yet that they need distinct model routing; `unconstrained`
  keeps the vocabulary honest without speculative branches. Revisit when a
  viewpoint shows a measurable routing win.
- **Keep capture requirements but enforce them softly.** Rejected: the
  product promise is zero videographer coordination; a "soft requirement"
  still reads as a rule and still generates support burden.
