# Staff person-kind replay HTTP regression

`gpu-worker/tests/integration/test_actor_kind_http.py` adds an actual isolated
cross-service regression for staff correction authority across tracking generations.
Only this new test and evidence/report files were owned by this slice; the parent
and position owner implemented the product changes it verifies.

The test uses the existing real loopback uvicorn/PostgreSQL fixture, authenticated
staff and configured processor accounts, a newly registered runtime and actual
claimed live pipeline leases. It uploads a uniquely generated synthetic protocol
video through HTTP, uses actual native PTS/storage/full source hashing and creates
explicit manual protocol observations. No production services, real film labels
or deployed worker are used. An explicit parsed loopback scratch-database
allowlist is checked before fixture seeding.

The worker's actual `fetch_annotation_snapshot` feeds the actual orchestrator
`accept('track')` replay. Six claimed/fenced HTTP completion cycles verify:

1. A false official candidate becomes a player only through the saved staff
   correction and exact original observation/source binding.
2. An explicit synthetic roster pick survives a later jersey-42/observed-defense
   edit without resubmitting the player ID. The binding is recaptured for the
   current track ID; manual identity is persisted before the ReID consumer.
3. A subsequent inherited-only snapshot still selects the current published
   track's `inherited_human` authority. Jersey, unit, manual identity and the
   actual human root label remain intact across another generation.
4. The next staff correction makes that actor an official, clears roster
   identity and supplies zero player tracks to the ReID and metrics consumers.
5. A persisted stale source binding causes a hold even when the new candidate
   starts as a player. It stays outside player consumers, the prior published
   generation remains available and actual `GET /overlays` explains the hold.
6. Missing legacy binding evidence causes the same conservative hold and warning.

Only the fixture's own current label is deliberately damaged for the two legacy
negative cases. No other database rows or jobs are modified. Each new job is
prioritized explicitly and its claimed ID is checked before execution. The
fixture's automatic upload job is cancelled by its own ID, and its private API
process is stopped by fixture teardown.

Segmentation and candidate generation are deterministic test seams, and the ReID
and metrics stages are input observers rather than learned model execution.
This verifies replay order and exclusion before player consumers, not CV accuracy
or the full learned inference chain. Source reads, timestamps, the snapshot,
actor replay, manual-identity HTTP patch, publication, current-label selection
and subsequent staff corrections are real. No metric or model approval is
invented, and the evidence explicitly records `football_validation:false`.

The final run passed in **47.33 seconds** including fixture setup/teardown; its
six-cycle test body took **36.95 seconds**. Source/hash-bound evidence is
`actor-kind-http-protocol.json`; the captured exact result is retained locally at
`runs/actor-kind-http-protocol-pytest.log` from tool session 72739. The test source
remained unchanged after that execution. Scoped Ruff, formatting and compilation
passed. Independent source review accepted the boundaries and assertions; it did
not start a competing database rerun while the parent's full backend checks were
pending. The parent owns subsequent combined-suite execution and master records.
