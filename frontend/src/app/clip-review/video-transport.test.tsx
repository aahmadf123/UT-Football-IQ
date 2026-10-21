import { afterEach, describe, expect, test, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { PLAYBACK_RATES, VideoTransport, frameStepTarget, toElementTime } from "./video-transport";

afterEach(cleanup);

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

describe("playback boundaries", () => {
  test("replays from the clip start after reaching its end", () => {
    const element = document.createElement("video");
    element.currentTime = 13;
    element.play = vi.fn().mockResolvedValue(undefined);
    render(<VideoTransport videoRef={{ current: element }} fps={30} clipOffset={10} duration={3} currentLocalTime={3} eventTimes={[]} />);
    fireEvent.click(screen.getByTestId("transport-play"));
    expect(element.currentTime).toBe(10);
    expect(element.play).toHaveBeenCalledOnce();
  });

  test("parent footage pauses at the play boundary on decoded frames", () => {
    const element = document.createElement("video");
    element.currentTime = 10;
    Object.defineProperty(element, "paused", { value: false });
    element.pause = vi.fn();
    let callback: VideoFrameRequestCallback | null = null;
    element.requestVideoFrameCallback = vi.fn((next: VideoFrameRequestCallback) => { callback = next; return 1; });
    element.cancelVideoFrameCallback = vi.fn();
    const { unmount } = render(<VideoTransport videoRef={{ current: element }} fps={30} clipOffset={10} duration={3} currentLocalTime={0} eventTimes={[]} enforceBounds />);
    element.currentTime = 13.01;
    act(() => { if (callback) (callback as VideoFrameRequestCallback)(0, { mediaTime: 13.01 } as VideoFrameCallbackMetadata); });
    expect(element.pause).toHaveBeenCalledOnce();
    expect(element.currentTime).toBeCloseTo(12.97);
    unmount();
    expect(element.cancelVideoFrameCallback).toHaveBeenCalled();
  });

  test("frame steps use the live playhead instead of the throttled readout", () => {
    const element = document.createElement("video");
    element.currentTime = 10.8;
    element.pause = vi.fn();
    render(<VideoTransport videoRef={{ current: element }} fps={30} clipOffset={10} duration={3} currentLocalTime={0} eventTimes={[]} />);
    fireEvent.click(screen.getByTestId("transport-frame-forward"));
    expect(element.currentTime).toBeGreaterThan(10.7);
  });

  test("Space on the shortcut summary opens help instead of toggling playback", () => {
    const element = document.createElement("video");
    element.play = vi.fn().mockResolvedValue(undefined);
    render(<VideoTransport videoRef={{ current: element }} fps={30} clipOffset={0} duration={3} currentLocalTime={0} eventTimes={[]} />);
    const summary = screen.getByLabelText("Playback keyboard shortcuts");
    const notCancelled = fireEvent.keyDown(summary, { key: " " });
    expect(notCancelled).toBe(true);
    expect(element.play).not.toHaveBeenCalled();
  });
  test("signed URL renewal mirrors the retained element playback speed", () => {
    const element = document.createElement("video");
    render(<VideoTransport videoRef={{ current: element }} fps={30} clipOffset={0} duration={3} currentLocalTime={0} eventTimes={[]} />);
    element.playbackRate = 1.5;
    fireEvent(element, new Event("ratechange"));
    expect((screen.getByTestId("transport-speed") as HTMLSelectElement).value).toBe("1.5");
  });
});
