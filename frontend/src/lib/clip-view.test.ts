import { describe, expect, test } from "vitest";
import type { ApiClip } from "./types";
import { clipView, sameplayAngles, situationLabel } from "./clip-view";

function clip(id: string, play: number, view: string | null, start: number, extra: Partial<ApiClip> = {}): ApiClip {
  return {
    id,
    video_id: "v",
    start_time: start,
    end_time: start + 10,
    play_number: play,
    is_reviewed: false,
    label_data: view ? { view } : null,
    created_at: "2026-10-16T00:00:00Z",
    ...extra,
  };
}

describe("clip view helpers", () => {
  test("pairs the angles of one play in film order", () => {
    const side = clip("a", 3, "sideline", 30);
    const end = clip("b", 3, "endzone", 42);
    const other = clip("c", 4, "sideline", 60);
    expect(sameplayAngles(end, [other, end, side]).map((c) => c.id)).toEqual(["a", "b"]);
    expect(clipView(side)).toBe("sideline");
    // Practice clips carry no view: no angle switch.
    expect(sameplayAngles(clip("p", 1, null, 0), [clip("p", 1, null, 0)])).toEqual([]);
  });

  test("formats the scoreboard situation", () => {
    const c = clip("a", 1, "sideline", 0, {
      down: 3,
      distance: 7,
      label_data: { view: "sideline", scoreboard: { quarter: 2, clock: "4:12", ball_on: 35 } },
    });
    expect(situationLabel(c)).toBe("3rd & 7 · Q2 4:12 · Ball on 35");
    const goal = clip("g", 2, "endzone", 0, {
      label_data: { view: "endzone", scoreboard: { down: 1, goal_to_go: true, quarter: 5 } },
    });
    expect(situationLabel(goal)).toBe("1st & Goal · OT");
    expect(situationLabel(clip("n", 1, null, 0))).toBeNull();
  });
});
