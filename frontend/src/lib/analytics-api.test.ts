import { afterEach, expect, test, vi } from "vitest";
import { queryAnalytics } from "./analytics-api";
import { ANALYSIS_DEFAULTS } from "./analysis-context";
vi.mock("./endpoints", () => ({ apiBase: () => "" }));
afterEach(() => vi.unstubAllGlobals());

test("the cohort request carries session, drill, contact and football context from the shared URL", async () => {
  const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({ groups: [] }), { status: 200 }));
  vi.stubGlobal("fetch", fetcher);
  await queryAnalytics({ ...ANALYSIS_DEFAULTS, session_ids: "session-a, session-b", drill_type: "7on7", contact_level: "thud", opponent: "Buffalo", personnel: "11", formation: "Trips", situation: "third_down", since: "2026-10-01", until: "2026-10-21" }, "staff");
  expect(JSON.parse(fetcher.mock.calls[0][1].body)).toMatchObject({ session_ids: ["session-a", "session-b"], drill_type: "7on7", contact_level: "thud", opponent: "Buffalo", personnel: "11", formation: "Trips", situation: "third_down", since: "2026-10-01T00:00:00Z", until: "2026-10-21T23:59:59.999999Z" });
});
