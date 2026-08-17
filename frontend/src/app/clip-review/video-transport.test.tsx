import { describe, expect, test } from "vitest";
import { PLAYBACK_RATES, frameStepTarget, toElementTime } from "./video-transport";

describe("frameStepTarget", () => {
  test("steps forward one frame landing mid-frame at 30fps", () => {
    // At frame 30 (1.0s), stepping +1 targets frame 31 → (31 + 0.5) / 30.
    expect(frameStepTarget(1.0, 30, 1)).toBeCloseTo(31.5 / 30, 5);
  });

  test("steps backward one frame", () => {
    expect(frameStepTarget(1.0, 30, -1)).toBeCloseTo(29.5 / 30, 5);
  });

  test("never goes below zero", () => {
    expect(frameStepTarget(0, 30, -1)).toBe(0);
  });

  test("works at non-integer positions (codec rounding)", () => {
    // 0.9833s at 30fps is frame 29.499 → floors to 29; +1 → frame 30.
    expect(frameStepTarget(0.9833, 30, 1)).toBeCloseTo(30.5 / 30, 5);
  });

  test("respects the fps of the footage, not a hardcoded rate", () => {
    expect(frameStepTarget(1.0, 60, 1)).toBeCloseTo(61.5 / 60, 5);
  });
});

describe("toElementTime", () => {
  test("parent-video playback shifts by the clip offset", () => {
    expect(toElementTime(2.5, 10)).toBe(12.5);
  });

  test("rendered clip assets play in their own timebase", () => {
    expect(toElementTime(2.5, 0)).toBe(2.5);
  });
});

describe("PLAYBACK_RATES", () => {
  test("covers slow-motion review and fast scan around 1×", () => {
    expect(PLAYBACK_RATES).toContain(1);
    expect(Math.min(...PLAYBACK_RATES)).toBeLessThan(1);
    expect(Math.max(...PLAYBACK_RATES)).toBeGreaterThan(1);
  });
});
