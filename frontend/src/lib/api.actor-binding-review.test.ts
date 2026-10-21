import { afterEach, expect, test, vi } from "vitest";
import { fetchActorBindingReview, removeInvalidReviewedEvent, updateReviewedEvent } from "./api";

afterEach(() => vi.unstubAllGlobals());
test("binding review reads and existing-event updates/removal carry staff auth and optimistic revision", async () => {
  const fetcher = vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => ({ annotation_revision: 0, observations: [] }) });
  vi.stubGlobal("fetch", fetcher);
  await fetchActorBindingReview("play space", "staff-token");
  await updateReviewedEvent("saved event", { actor_id: "selected", shot_id: "0" }, "staff-token", 0, 0);
  await removeInvalidReviewedEvent("saved event", "staff-token", 1);
  const calls = fetcher.mock.calls as [string, RequestInit][];
  expect(calls[0][0]).toContain("/clips/play%20space/actor-binding-review");
  expect(calls[1][0]).toContain("/events/saved%20event");
  expect(calls[1][1].method).toBe("PATCH");
  expect(JSON.parse(String(calls[1][1].body))).toEqual({ attributes: { actor_id: "selected", shot_id: "0" }, expected_revision: 0, timestamp_seconds: 0 });
  expect(calls[2][0]).toContain("/events/saved%20event?expected_revision=1");
  expect(calls[2][1].method).toBe("DELETE");
  for (const [, options] of calls) expect(options.headers).toMatchObject({ Authorization: "Bearer staff-token" });
});
