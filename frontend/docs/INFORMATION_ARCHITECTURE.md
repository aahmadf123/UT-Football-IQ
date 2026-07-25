# Frontend information architecture

This is the coach-facing navigation map after the IA cleanup (ADR 0003,
Issues #184–#187). The authoritative rationale lives in
[`docs/adr/0003-coach-facing-information-architecture.md`](../../docs/adr/0003-coach-facing-information-architecture.md).

## Primary navigation (sidebar)

| Nav item | Route | Notes |
|---|---|---|
| Dashboard | `/` | Cockpit + Practice Inbox |
| **Film Room** | `/film-room` | Tabs: Browse Film · Review & Tag Plays · Clips & Highlights · Upload / Process Film |
| **Scouting** | `/scouting` | Tabs: Our Tendencies · Opponent Prep · College Data |
| Players | `/players` | |
| Player Development | `/player-development` | |
| Health & Workload | `/health-workload` | |
| **Model Insights** | `/analytics` | Renamed from "Analytics"; home of future #141 What-If UI |
| Reports | `/reports` | |
| Settings | `/settings` | |

**Alerts** is a top-bar bell/inbox (the `/alerts` page), not a sidebar item.

## Tab deep links

- Film Room: `?tab=browse` (default) · `?tab=review` · `?tab=clips` · `?tab=upload`
- Scouting: `?tab=tendencies` (default) · `?tab=opponent` · `?tab=college`

## Compatibility routes

These keep working (deep links / bookmarks) but are no longer in the sidebar:

| Old route | Behaviour |
|---|---|
| `/video-and-plays` | redirects → `/film-room?tab=review` |
| `/clips-highlights` | redirects → `/film-room?tab=clips` |
| `/model-insights` | redirects → `/analytics` |
| `/library` | renders Browse Film (same component the hub uses) |
| `/self-scout` | renders Our Tendencies |
| `/opponent-scout` | renders Opponent Prep |
| `/college-data` | renders College Data |
| `/clip-review?clipId=…` | unchanged — deep-linked clip review surface |

The app is a Next static export (`output: "export"`), so redirects are
client-side (`src/components/route-redirect.tsx`).

## Upload → processing (Issue #187)

Uploading film **never auto-enqueues processing**. Uploaded film appears in
Film Room → Upload / Process Film with a **Process Film** CTA. The CTA calls the
backend job API (`POST /api/v1/jobs`, workload-gated) to create an `ingest`
job — it does not bypass the backend or the upload flow. The lifecycle is
surfaced in coach language:

```
Uploaded → Queued → Processing → Processed
                              ↘ Failed → Retry
```

A `503 workload_gated` response is shown as a "system is busy" message.
