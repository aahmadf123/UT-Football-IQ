import { describe, expect, test } from "vitest";
import { filmReviewHref, playerProfileContextHref, resolveAnalysisContext, safeReturnHref, updateContextParams } from "./analysis-context";

describe("shared analysis context", () => {
  test("return links cannot navigate outside the workspace", () => {
    expect(safeReturnHref("/\\evil.test", "/film-room/")).toBe("/film-room/");
    expect(safeReturnHref("//evil.test", "/film-room/")).toBe("/film-room/");
    expect(safeReturnHref("/team-stats/?position=TE", "/film-room/")).toBe("/team-stats/?position=TE");
  });
  test("explicit empty URL filters override saved and personal defaults", () => {
    expect(resolveAnalysisContext(new URLSearchParams("position=&layout=analyst"), { position: "RB", season: "2026" }, { position: "QB", season: "2025" })).toMatchObject({ position: "", season: "2026", layout: "analyst" });
  });
  test("next play preserves cohort and Back, resetting only play-local time", () => {
    const url = new URL(filmReviewHref(new URLSearchParams("clipId=a&t=7&position=TE&from=%2Fteam-stats%2F&playlist=a,b&layout=analyst"), "b"), "https://film.test");
    expect(url.searchParams.get("position")).toBe("TE");
    expect(url.searchParams.get("from")).toBe("/team-stats/");
    expect(url.searchParams.get("playlist")).toBe("a,b");
    expect(url.searchParams.has("t")).toBe(false);
  });
  test("layout changes preserve selected play and timestamp", () => {
    const params = updateContextParams(new URLSearchParams("clipId=a&t=7.5&session_ids=s"), { layout: "analyst" });
    expect(params.get("t")).toBe("7.5");
    expect(params.get("clipId")).toBe("a");
  });
  test("malformed saved values cannot crash date or roster filters", () => {
    const saved = { since: { nested: true }, position: 7 } as unknown as Partial<import("./analysis-context").AnalysisContext>;
    expect(resolveAnalysisContext(new URLSearchParams(), saved)).toMatchObject({ since: "", position: "" });
  });
  test("profile switches carry drill and cohort context with a stable safe return link", () => {
    const url = new URL(playerProfileContextHref(new URLSearchParams("clipId=a&t=7&drill_type=7on7&contact_level=thud&season=2026"), "player-b", "/team-stats/?drill_type=7on7"), "https://film.test");
    expect(url.searchParams.get("player_id")).toBe("player-b");
    expect(url.searchParams.get("drill_type")).toBe("7on7");
    expect(url.searchParams.get("contact_level")).toBe("thud");
    expect(url.searchParams.get("from")).toBe("/team-stats/?drill_type=7on7");
    expect(url.searchParams.has("clipId")).toBe(false);
    expect(url.searchParams.has("t")).toBe(false);
  });
});
