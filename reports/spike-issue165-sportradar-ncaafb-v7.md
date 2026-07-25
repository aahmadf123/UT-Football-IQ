# Spike — Issue #165: Sportradar NCAAFB API v7 for production-grade live college-football data

**Status:** Spike complete — recommendation reached. No production code shipped.
**Type:** Vendor evaluation (documentation only).
**Parent:** [#160](https://github.com/aahmadf123/Football-IQ/issues/160) · **Governance:** [#166](https://github.com/aahmadf123/Football-IQ/issues/166)
**Last updated:** May 2026.

---

## 0. TL;DR recommendation

**Do not adopt Sportradar NCAAFB API v7 now. Defer behind a concrete live
in-game product decision.**

Sportradar genuinely fills a *live-data* gap that CFBD does not (real-time game
status and ~2 s play-by-play during a live game). But Football-IQ today is a
**film-after-the-fact** video-intelligence product: coaches upload practice and
game film and the GPU pipeline does the analysis. Nothing in the current
architecture or roadmap consumes a live external play-by-play stream. The one
gap Sportradar uniquely covers is therefore not a gap the product currently
has, and it comes with a paid B2B contract where CFBD covers the
historical/scouting needs for free.

Keep **CFBD as the authoritative college-football data source** (Issues #160–#163
remain canonical). Re-open the Sportradar question only when a live in-game
feature is actually scoped (see §9 for the single proposed follow-up). This
spike defines the backend-only secret pattern and access-confirmation checklist
so that re-evaluation is cheap when/if that day comes.

---

## 1. Spike scope and honest limitations

This spike answers the questions in #165 from **public Sportradar developer
documentation**. Two limitations are stated up front so the reader does not
over-trust the result:

1. **No Sportradar credentials are provisioned.** There is no Sportradar trial
   or production key in `.env.example`, the backend config, GitHub Actions
   secrets, or this environment (only `CFBD_*` and `KAGGLE_*` exist). Per the
   spike's own rules (no secrets committed, no client-side calls, no production
   code beyond a tiny proof-of-access), **no live API call was made.** "Which
   endpoints are available under current account access" therefore cannot be
   *confirmed against a live dashboard here* — §3 documents the full public
   feed set and §8 gives the exact checklist the account holder runs against
   their dashboard to confirm entitlements.
2. **Doc-derived figures may drift.** Rate limits, TTLs, and latency tiers
   below are from current public docs (May 2026) and the developer changelog;
   the contracted production QPS is whatever the signed package specifies, not
   the trial default. Treat the numbers as the basis for a decision, not a
   contract.

---

## 2. Why this is even a question

Football-IQ is Toledo/MAC-first American football. CFBD already ships as the
baseline historical/cached source (`backend/app/cfbd/`, `cfbd_*` Postgres cache
tables in migration 0016, read-only `/api/cfbd/*`). The #165 hypothesis is that
Sportradar might cover *production-grade live/reliable* data that CFBD does not:
live game status, real-time play-by-play, official schedules/scores, and richer
team/player profiles. This spike tests that hypothesis endpoint-by-endpoint.

---

## 3. NCAAFB API v7 — feed inventory (from public docs)

The NCAAFB API v7 is organized into ~20 focused **feeds**. Most are RESTful
(poll on demand); **Push feeds** (streaming) are available only to Realtime
customers. The useful feeds for a college-football product, grouped:

### Schedules & scores
| Feed | Purpose | Update cadence (docs) |
|---|---|---|
| Current Season Schedule | Full current-season schedule incl. venue, broadcast, results by quarter | Per-game close |
| Season Schedule | Full schedule for a given year | Static post-season |
| Current Week Schedule | Current week incl. venue, weather, broadcast | Weekly |
| Weekly Schedule | Schedule for a given year/week | Weekly |

### Live game data
| Feed | Purpose | Update cadence (docs) |
|---|---|---|
| Game Boxscore | Detailed team scoring + scoring-drive play-by-play | Live during game |
| Game Statistics | Full team + player game statistics | Live during game |
| **Game Play-by-Play** | Every play, real-time | **2 s TTL once `inprogress`**; expected latency tier 2/10/25/50 s |
| Game Roster | Declared active roster + live `in_game_status` (active/probable/questionable/doubtful/benched/out/unknown) | Real-time during game |
| Game Status workflow | `scheduled → inprogress → closed/complete` lifecycle states | Live |

### Teams & players
| Feed | Purpose | Update cadence (docs) |
|---|---|---|
| League Hierarchy | Conferences → divisions → teams tree | Seasonal |
| Team Roster | Full roster for a team | Maintained from summer pre-season |
| Team Profile | Team metadata | Seasonal |
| Player Profile | Bio, draft info, seasonal stats | Seasonal / post-game |
| Seasonal Statistics | Team + player seasonal stats (back to 2013, varies by player/team) | ~5 min after game `closed`; 120 s TTL |

### Rankings, standings, ops
| Feed | Purpose | Update cadence (docs) |
|---|---|---|
| Rankings (Current Week) / Rankings (By Week) | AP/Coaches top-25 polls; CFP after week 10 | Within 15 min of release (Sun; CFP Tue) |
| Standings | Conference/division standings | Per-game/weekly |
| Daily Change Log | Compact diff of changes to teams, players, stats, schedules, standings | Live; designed to save quota |
| Push Events / Push (Realtime) | Streaming live events (single long-lived connection) | Realtime-tier only |

### Explicitly out of scope (per #165 and product fit)
NFL API, Images API, Odds API, betting/prop feeds, and **Push feeds** are out of
scope. NFL API: Football-IQ is college-first. Images API: licensing/cost with no
product need. Odds/betting feeds: governance + no product use; recruiting/coaching
tool, not a betting product. Push feeds: Realtime-tier upsell only relevant if a
live feature is greenlit (§9), and even then polling Play-by-Play at 2 s TTL is
the simpler first step.

---

## 4. Access model, auth, and limits (from public docs)

- **URL shape:** `https://api.sportradar.com/ncaafb/official/{access_level}/v7/{lang}/...{format}`
  where `access_level ∈ {trial, production}`, `lang = en`, `format ∈ {json, xml}`.
  (Confirmed shape from the NFL v7 analogue, e.g.
  `.../nfl/official/trial/v7/en/games/2024/REG/schedule.json`.)
- **Auth:** API key. Current portal uses the **`x-api-key` request header**;
  legacy/simulation examples accept an `api_key` **query parameter**. **Prefer
  the header** so the key never lands in URLs, proxy logs, or access logs.
  **Backend-only** — Sportradar's own guidance is that these are B2B services
  and must not be called from client applications.
- **Trial constraints:** 30 days, **1,000 total calls**, **1 QPS**. Data
  freshness is *not* throttled vs production — only call volume/rate is. A trial
  is enough to confirm entitlements and shapes, **not** enough to run anything
  live in production (a single live game polled at 2 s TTL would exhaust 1,000
  calls in well under an hour).
- **Production QPS:** per-product, set by the signed package — visible on the
  account dashboard. Not a fixed public number.
- **Caching contract:** respect the documented TTLs (2 s live PBP, 120 s
  seasonal stats, etc.) and use the **Daily Change Log** to avoid blind polling.

---

## 5. Endpoint-by-endpoint comparison vs CFBD

CFBD today (`backend/app/cfbd/client.py`): `teams`, `games`, `drives`, `plays`
(year+week, **post-game/historical**), `games/teams` (box score), `metrics/wp`
(win probability). All ingested into Postgres cache tables; **no live vendor
call sits in the request path**.

| Need | Sportradar NCAAFB v7 | CFBD equivalent | Verdict |
|---|---|---|---|
| Schedules / scores | Season/Weekly Schedule | `get_games` | **Overlap.** CFBD is sufficient and free. |
| Final box score | Game Boxscore / Game Statistics | `get_team_game_stats` | **Overlap.** CFBD covers post-game. |
| Historical play-by-play | Game Play-by-Play (also historical) | `get_plays` | **Overlap.** CFBD covers historical PBP for scouting. |
| Drives | scoring drives in Boxscore | `get_drives` | **Overlap.** CFBD sufficient. |
| Win probability | (derive from PBP) | `get_win_probability` (`metrics/wp`) | **CFBD better** — ready-made WP, no derivation. |
| Teams / hierarchy | League Hierarchy / Team Profile | `get_teams` | **Overlap.** CFBD sufficient for MAC. |
| Rosters / player profiles | Team Roster, Player Profile, Game Roster | (not currently ingested) | **Sportradar richer**, but no current product consumer. |
| Seasonal statistics | Seasonal Statistics (2013+) | partial via game stats | **Roughly even**; no gap forcing a switch. |
| Rankings / standings | Rankings, Standings | partial | **Minor Sportradar edge**; not decision-driving. |
| **Live game status** | Game Status workflow | **none** (CFBD is post-game) | **Sportradar only.** Real gap — *no current consumer.* |
| **Real-time play-by-play (~2 s)** | Game Play-by-Play live | **none** | **Sportradar only.** Real gap — *no current consumer.* |
| **Live player availability** | Game Roster `in_game_status` | **none** | **Sportradar only.** Real gap — *no current consumer.* |

**Reading of the table:** Everything Football-IQ actually consumes today is
**overlap** that CFBD already covers (and for win probability CFBD is *better*
out of the box). The three places where Sportradar is uniquely capable are all
**live in-game** features that **the product does not currently have**.

---

## 6. Does Sportradar fill a *production* gap CFBD misses?

**Technically yes, practically no — not today.**

- The unique value is live (~2 s) game status, play-by-play, and player
  availability during games in progress.
- Football-IQ's data flow is `Coach → Frontend → Worker → R2 → Queue → GPU
  Worker → Backend → Postgres`. It analyzes **uploaded film** after the fact.
  There is no live-game surface, no live scoreboard, no in-game win-probability
  ticker, and no roadmap item in the read docs that consumes a live external
  feed.
- For the historical/scouting context the product *does* use, CFBD is adequate,
  free, already integrated, and already cached for resilience.

So the live gap is real but **unmatched to any current product requirement**.
Adopting now would mean paying for a B2B contract to fill a gap the product
does not yet have.

---

## 7. Recommendation (one of: do not use / fallback / live-only / premium replacement)

**Primary: Do not use now — defer.** Keep CFBD authoritative. Revisit only
behind a scoped live in-game feature.

For completeness, the other options and why they lose today:

- **Use as fallback for schedules/scores:** rejected. CFBD already caches into
  Postgres and rows survive a CFBD outage; a paid second source for redundancy
  on free, non-critical data is not justified.
- **Use live-only (live PBP/status):** rejected *for now* — this is exactly the
  capability to buy *if* a live feature is greenlit (§9). It is the right answer
  the moment a live consumer exists; it is premature without one.
- **Premium replacement of CFBD:** rejected. No quality gap justifies replacing
  a free, integrated, MAC-adequate source with a paid contract, and it would
  rewrite shipped #160–#163 work for no product gain.

---

## 8. If/when adopted — backend-only secret & config pattern

This is the pattern to implement **only after** §9's decision, mirroring the
CFBD precedent. Nothing here is wired in this spike.

**Proposed secret names (no values committed, ever):**

| Env var | Purpose |
|---|---|
| `SPORTRADAR_API_KEY` | Master API key for the NCAAFB v7 product. Backend-only. |
| `SPORTRADAR_BASE_URL` | Default `https://api.sportradar.com`. |
| `SPORTRADAR_ACCESS_LEVEL` | `trial` or `production` (path segment). |
| `SPORTRADAR_NCAAFB_VERSION` | Default `v7`. |

Pattern requirements (carried from CFBD + repo hard constraints):
- **Backend-only.** Read only by the FastAPI backend / ingestion jobs. **Never**
  exposed to frontend, browser bundles, logs, PR/issue text,
  R2 artifacts, or coach-visible errors; never stored in the database. Send the
  key as the **`x-api-key` header**, not a query parameter.
- **Config wiring (deferred):** add the vars to `.env.example` *and* to
  `backend/app/config.py` `Settings` at implementation time (they are placeholders
  only in this spike — see §10). Build a client mirroring `CFBDClient`
  (`from_settings`, structlog that never emits the key/header, backoff that honors
  `Retry-After` and the documented TTLs).
- **Storage shape:** ingest into separate `sportradar_*` cache tables rather than
  overloading `cfbd_*` (different IDs/semantics; keeps provenance clean and lets
  the two sources be diffed). A live feature would additionally need a polling/
  Change-Log loop, not a request-path vendor call.
- **Governance (#166/#166-rubric):** any coach-visible Sportradar surface goes
  behind `require_policy(...)`; live polling endpoints, if added, are heavy and
  use `require_workload_capacity(...)`. New routes live under
  `backend/app/routers/` and mount in `main.py`.

**Account-access confirmation checklist (run by the key holder, not in CI):**
1. Log into the Sportradar dashboard; confirm the **NCAAFB** product (not NFL)
   is entitled and note trial vs production + the configured QPS.
2. With the key in an env var locally (never committed), `curl` one read feed
   via the `x-api-key` header, e.g. League Hierarchy or Current Week Schedule,
   `format=json`, to confirm `200` + shape.
3. Record which feeds return `403`/not-entitled vs `200` and paste the
   entitlement list (not the key) back onto #165.

---

## 9. Proposed follow-up (only if justified)

Exactly **one** follow-up is justified, and it is **blocked/deferred**, not an
implementation issue:

> **Proposed issue — "Live in-game data: scope before evaluating Sportradar
> live feeds"** *(blocked on a product decision).* When and if a live in-game
> feature is greenlit (e.g., live opponent scoreboard, in-game win-probability
> ticker, live player-availability board), re-open the Sportradar NCAAFB v7
> evaluation for the **live-only** option using §8's pattern and §8's
> confirmation checklist. Until a live consumer exists, this stays closed/parked.

No ingestion, client, or schema issues are proposed — adopting any would
contradict the recommendation.

---

## 10. Governance rubric (#166) — Sportradar NCAAFB API v7

| Field | Detail |
|---|---|
| **Sport coverage** | American / college football ✅ (NCAA FB). Not soccer. |
| **Toledo / MAC relevance** | Broad college football incl. MAC; no Toledo-specific advantage over CFBD established. |
| **Source URL** | https://developer.sportradar.com/football/docs/ncaafb-ig-api-basics |
| **License / access terms** | Commercial B2B contract. Trial: 30 days / 1,000 calls / 1 QPS. Production QPS per signed package. Not redistributable; respect TTLs. |
| **Runtime category** | Production API (backend-only) — **evaluated, not adopted.** Documentation only in this spike. |
| **Secret / key requirement** | Proposed `SPORTRADAR_API_KEY` (+ `SPORTRADAR_BASE_URL`, `SPORTRADAR_ACCESS_LEVEL`, `SPORTRADAR_NCAAFB_VERSION`). Backend-only; `x-api-key` header; never frontend/logs/DB/PR text. |
| **Data privacy risk** | Public team/game stats + player availability statuses. No medical/wellness data. Player availability is publicly reported game-day status, not protected health info — but treat as not-for-logging. |
| **Model-router / registry path** | N/A — data integration, not an inference model. |
| **Overlap with closed decisions** | Single-camera (#101), pgvector (#8/#77), SAM (#74) untouched. CFBD (#160–#163) remains the authoritative college-data source; this spike explicitly does **not** replace it. |
| **Calibrated-tracking dependency** | None — no calibrated-tracking assumption (#127/#128/#129 not implicated). |
| **Decision** | **Not adopted.** Defer behind a live-feature product decision (§9). |

---

## 11. Acceptance criteria status (#165)

- [x] Confirm available products/endpoints — full public feed set documented (§3);
  live dashboard confirmation deferred to the key holder with an exact checklist
  (§8) because no credentials are provisioned and the spike forbids committing
  secrets / calling live.
- [x] Document the NCAAFB v7 endpoints worth using (§3) and access limits (§4).
- [x] Compare each useful endpoint against CFBD (§5).
- [x] Recommendation: **do not use now / defer** (§7).
- [x] Propose secret names without exposing values (§8).
- [x] Follow-up issues proposed only if justified — one deferred/blocked
  follow-up (§9).

---

## 12. Sources

- [NCAAFB API Basics](https://developer.sportradar.com/football/docs/ncaafb-ig-api-basics)
- [Make Your First Call](https://developer.sportradar.com/getting-started/docs/make-your-first-call)
- [API Authentication & Headers](https://developer.sportradar.com/getting-started/docs/authentication)
- [NCAA Football Update Frequencies](https://developer.sportradar.com/football/docs/ncaafb-ig-update-frequencies)
- [NCAA Football Game Status Workflow](https://developer.sportradar.com/football/docs/ncaafb-ig-game-status-workflow)
- [Live Game Updates](https://developer.sportradar.com/football/docs/ncaafb-ig-live-game-retrieval)
- [Seasonal Statistics](https://developer.sportradar.com/football/docs/ncaafb-ig-seasonal-stats)
- [Standings and Rankings](https://developer.sportradar.com/football/docs/ncaafb-ig-standings-rankings-retrieval)
- [Rosters](https://developer.sportradar.com/football/docs/ncaafb-ig-rosters)
- [Game Expected Latency (changelog)](https://developer.sportradar.com/sportradar-updates/changelog/ncaa-football-api-game-expected-latency)
- [Your Account / trial limits](https://developer.sportradar.com/getting-started/docs/your-account)
