import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";
import {
  isBackgroundRefreshPaused,
  pauseBackgroundRefresh,
  resumeBackgroundRefresh,
} from "./background-refresh";
import { createMediaClock, useMediaTime, useThrottledMediaTime } from "./media-clock";

afterEach(() => vi.useRealTimers());

describe("media clock", () => {
  test("per-frame subscribers see every change", () => {
    const clock = createMediaClock();
    const { result } = renderHook(() => useMediaTime(clock));
    act(() => clock.set(1.5));
    expect(result.current).toBe(1.5);
  });

  test("throttled subscribers update at most once per interval, then land on the last time", () => {
    vi.useFakeTimers();
    const clock = createMediaClock();
    let renders = 0;
    const { result } = renderHook(() => {
      renders += 1;
      return useThrottledMediaTime(clock, 250);
    });
    const before = renders;
    act(() => {
      for (let frame = 1; frame <= 30; frame += 1) clock.set(frame / 30);
    });
    expect(renders - before).toBeLessThanOrEqual(2);
    act(() => {
      vi.advanceTimersByTime(300);
    });
    expect(result.current).toBe(1);
  });

  test("background refresh pauses while any player is playing", () => {
    pauseBackgroundRefresh("a");
    pauseBackgroundRefresh("b");
    resumeBackgroundRefresh("a");
    expect(isBackgroundRefreshPaused()).toBe(true);
    resumeBackgroundRefresh("b");
    expect(isBackgroundRefreshPaused()).toBe(false);
  });
});
