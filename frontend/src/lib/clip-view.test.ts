import { describe, expect, test } from "vitest";
import type { ApiClip } from "./types";
import { alignedViewTime, clipView, playSequence, sameplayAngles, situationLabel } from "./clip-view";

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
  test("angle offsets use canonical time, and independent views retain clamped local time", () => {
    expect(alignedViewTime(5, 2, 4, 10)).toBe(3);
    expect(alignedViewTime(5, null, null, 10)).toBe(5);
    expect(alignedViewTime(5, 0, 9, 10)).toBe(0);
    expect(alignedViewTime(15, null, null, 10)).toBeCloseTo(9.99);
  });
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

  test("preserves coach-cleared fields and prefers corrected distance to OCR goal", () => {
    const cleared = clip("cleared", 1, "sideline", 0, {
      down: null,
      distance: null,
      label_data: { view: "sideline", scoreboard: { down: 3, distance: 7 } },
    });
    expect(situationLabel(cleared)).toBeNull();

    const corrected = clip("corrected", 1, "sideline", 0, {
      down: 3,
      distance: 5,
      label_data: { view: "sideline", scoreboard: { down: 1, goal_to_go: true } },
    });
    expect(situationLabel(corrected)).toBe("3rd & 5");
  });
});

describe("playSequence", () => {
  const film = [
    clip("1s", 1, "sideline", 0),
    clip("1e", 1, "endzone", 10),
    clip("2s", 2, "sideline", 20),
    clip("2e", 2, "endzone", 30),
    clip("3e", 3, "endzone", 40), // the sideline shot of play 3 was missed
  ];
  const ids = (current: ApiClip) => playSequence(current, film).map((c) => c.id);

  test("steps play to play on the camera being watched", () => {
    expect(ids(film[0])).toEqual(["1s", "2s", "3e"]);
    expect(ids(film[1])).toEqual(["1e", "2e", "3e"]);
  });

  test("next from the end zone shot of play 1 is play 2, not the same play again", () => {
    const steps = ids(film[1]);
    expect(steps[steps.indexOf("1e") + 1]).toBe("2e");
  });

  test("clips without a play number each stay a step", () => {
    const practice = [clip("a", 1, null, 0, { play_number: null }), clip("b", 1, null, 5, { play_number: null })];
    expect(playSequence(practice[0], practice).map((c) => c.id)).toEqual(["a", "b"]);
  });
});
