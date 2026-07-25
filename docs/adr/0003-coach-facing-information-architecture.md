# ADR 0003 — Coach-facing information architecture: Film Room, Scouting, Model Insights, and explicit upload-to-processing

Status: **Accepted** (Issues #184, #185, #186, #187)
Date: 2026-05-30
Deciders: Football-IQ analytics/UX
Related: #141 (counterfactual "What-if" UI — future), #96 (no mock data as
real), #102/#103/#104 (clip review + metrics), #112 (settings), #137
(tendency-break alerts), #163 (CFBD analytics), ADR 0002 (field visualization)

## Context

The coach-facing Next.js app grew one flat sidebar item per feature as the
platform was built out (Library, Video & Plays, Clips & Highlights, Self-Scout,
Opponent Scout, Analytics, Alerts, College Data, …). Several of these are
different views of the *same* coach task and the flat list no longer reads like
a coaching workflow. Before Package 13 / #141 adds a new counterfactual
"What-if" surface, we want a coherent IA so the new surface has an obvious home
and coaches are not handed yet another top-level tab.

Four issues drive this ADR:

- **#184** — Library, Video & Plays, and Clips & Highlights are all "work with
  film". Consolidate into one **Film Room** destination.
- **#185** — Self-Scout and Opponent Scout are both "scout tendencies".
  Consolidate into one **Scouting** workspace.
- **#186** — Clarify what Analytics, Alerts, and College Data each own before
  more model surfaces land.
- **#187** — Make upload-to-processing explicit and coach-friendly.

This is an IA + product-flow change only. It adds **no** new model outputs and
makes **no** direct external-API calls from the browser (CFBD/Sportradar/Kaggle
stay behind the backend, per the repo guardrails).

## Decision

### 1. Film Room (consolidates Library, Video & Plays, Clips & Highlights — #184)

A single top-level **Film Room** destination (`/film-room`) with four tabs:

| Tab | Source view (reused, not rewritten) |
|---|---|
| **Browse Film** | the former Library (`LibraryView`) |
| **Review & Tag Plays** | the former Video & Plays clip-review/tagging view |
| **Clips & Highlights** | the former Clips & Highlights view |
| **Upload / Process Film** | new explicit upload + processing view (see #4) |

Tabs are selected with a `?tab=` query param so each tab is linkable. The
existing per-clip review surface (`/clip-review?clipId=…`) is unchanged and is
still the deep-link target from Browse Film, Alerts, and Scouting.

### 2. Scouting (consolidates Self-Scout and Opponent Scout — #185)

A single top-level **Scouting** workspace (`/scouting`) with tabs:

| Tab | Source view |
|---|---|
| **Our Tendencies** | the former Self-Scout view (includes the Tendency-Break Alerts card, #137) |
| **Opponent Prep** | the former Opponent Scout view |
| **College Data (CFBD)** | the former College Data view (see #3) |

Tendency-break / pattern-break alerts stay where coaches already generate them —
inside **Our Tendencies** — rather than becoming a separate tab, to keep the
workspace to three tabs.

### 3. Analytics, Alerts, and College Data ownership (#186)

- **Analytics → renamed "Model Insights".** Analytics is where *model-derived*
  numbers live (expected separation/yards/pressure, formation run/pass, model
  quality, coaching-alert rollups). "Model Insights" makes that ownership
  explicit and distinguishes it from raw film (Film Room) and tendencies
  (Scouting). The route stays `/analytics` for deep-link stability; `/model-insights`
  is added as an alias that redirects to it.
- **College Data belongs under Scouting, not Model Insights.** CFBD data
  (Toledo team, schedule, MAC benchmark) is **external reference context** — it
  is *not* a model output and *not* derived from Toledo film (the page already
  carries a "CFBD data is external context" label). It supports opponent/league
  prep, so it lives as a tab inside the Scouting workspace, not under the
  model-output surface. It is removed as its own top-level sidebar item.
- **Alerts becomes a top-bar inbox, not a primary sidebar destination.** Alerts
  are a notification stream, not a workflow you "go do". They move to a bell /
  inbox affordance in the top bar that opens the existing `/alerts` inbox page.
  The `/alerts` route is unchanged; only its placement in the IA changes.

### 4. Placement rule for the future #141 "What-if" / counterfactual UI

When #141 adds counterfactual / "What-if" play exploration, it is a **model
output** and therefore belongs under **Model Insights** (a new "What-If" tab or
card on `/analytics`). It must **not** be added as a new top-level sidebar item
and must **not** be wedged into Film Room or Scouting. This ADR does **not**
implement that UI; it only reserves its home.

### 5. Upload-to-processing is explicit (#187)

Today an uploaded video is registered with status `uploaded` and **nothing**
enqueues processing — film silently sits idle. That is the ambiguity #187 calls
out.

Decision: **uploaded film appears in Film Room → Upload / Process Film with a
clear "Process Film" call-to-action; processing is never auto-enqueued.** This
is the safer coach-facing behavior (a coach decides when GPU work runs) and it
makes the lifecycle visible:

```
Uploaded  →  Queued  →  Processing  →  Processed        (happy path)
                                   ↘   Failed  → Retry
```

- The "Process Film" CTA calls the **backend** job API (`POST /api/v1/jobs`,
  workload-gated) to create an `ingest` job at nightly (full-quality) priority.
  It does **not** bypass the backend job API or the upload flow, and it does
  not talk to any external API.
- A `503 workload_gated` response is surfaced as a coach-readable "system is
  busy" message rather than a raw error.
- The five states (`uploaded`, `queued`/`running`, `processing`, `processed`/
  `ready`, `failed`) are preserved and labelled in coach language. Failed films
  keep the existing job-retry affordance.

Same-session (period-break, 5–10 min) processing remains a separate fast-path
concern and is **not** wired into this general "Process Film" button; that is a
follow-up.

## Compatibility / redirects

- Old film-only routes redirect into the hub: `/video-and-plays →
  /film-room?tab=review`, `/clips-highlights → /film-room?tab=clips`.
- Routes that are deep-linked with parameters or carry existing test/bookmark
  contracts — `/library`, `/self-scout`, `/opponent-scout`, `/college-data` —
  are **retained as working compatibility targets** (reachable by URL, reusing
  the exact same view component the hub renders) but are removed from primary
  navigation. The app uses Next static export (`output: "export"`), so redirects
  are implemented client-side, not via server config.
- `/model-insights` redirects to `/analytics`.

## Consequences

- ✅ Sidebar drops from ~14 flat items to a workflow-shaped list: Dashboard,
  Film Room, Scouting, Players, Player Development, Health & Workload, Model
  Insights, Reports, Settings — with Alerts as a top-bar inbox.
- ✅ #141 has a documented home (Model Insights) before any code lands.
- ✅ Uploading film no longer silently stalls; the coach sees status and an
  explicit Process Film action.
- ➖ Two URLs can render the same content during the migration window (e.g.
  `/library` and `/film-room?tab=browse`). Accepted: deep links and bookmarks
  keep working; navigation funnels through the hubs.
- ➖ View components are shared between the old compatibility routes and the new
  hubs; they were *moved* to sibling component files, not duplicated.

## Follow-up

- Wire a same-session ("get it back this period") fast-path option into Process
  Film once the period-break UX is designed.
- Implement #141 What-If as a Model Insights tab.
- After a deprecation window, convert the retained compatibility routes
  (`/library`, `/self-scout`, `/opponent-scout`, `/college-data`) to redirects
  and delete the wrapper pages.
