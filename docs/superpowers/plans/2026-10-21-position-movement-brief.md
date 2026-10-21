# Remaining observed position products

Implement the remaining initial observable position products on spec pages15–16:
receiver separation through time and paired observed receiver/defender paths;
first movement, rush/pursuit paths and observed closing speed for defensive roles;
special-teams operation/hang time and landing location from explicit qualified
snap/kick/punt/landing events; TE block/chip/route transitions from explicit
qualified interaction events. Reuse existing deterministic motion, role, source
clock and `position_evidence` helpers. Do not invent possession, coverage
responsibility, assignment correctness, contact, routes or unseen players.

Own a new observed-position helper module and focused tests, plus the minimal
stage_metrics hook and central metric definitions needed to make these reachable.
Read existing `qb_observations.py`, `oline_observations.py` and
`position_evidence.py` first; preserve their frozen work. No orchestrator/model
router/training changes (expected-outcomes agent owns those); no backend routes
or frontend changes (root owns those). Coordinate with root before broader edits.

Use actual calibrated trajectories with measured direction, explicit role/team
and qualified event evidence. Enforce native PTS, same known shot, visibility and
200ms continuity; no bridging loss/cuts. Separation labels must say nearest
defender or explicit comparison, never infer coverage responsibility. Closing
speed is an observed distance derivative, not recognition/tackling quality.
No-event/hidden observations remain unknown. All new rows stay experimental
until held-out validation and approval. Include reusable scalar and measured
series/trajectory/event packets, event IDs, actor/opponent IDs, definition/version
and applicable opportunity kind. Root will add eligible film-linked views.

Keep one central metric registry. Use opportunities such as routes/pass-rush/
coverage/punts rather than implying all snaps are those denominators. Only emit
an opportunity observation when explicit participation evidence exists. Preserve
anonymous team geometry where supported; player rates need resolved identity.

Read repo guardrails and spec. Test first; no helpers, commits, push/deploy,
new dependencies/downloads, synthetic production measurements or claims of actual
football accuracy. Self review, run relevant/full checks, and write report to
`reports/overhaul/position-movement-continuation.md`. Return an early payload
contract so root can wire the actual views in parallel.
