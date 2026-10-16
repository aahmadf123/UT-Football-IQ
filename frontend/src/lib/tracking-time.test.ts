import { describe, expect, test } from "vitest";
import type { OverlayTracklet } from "./types";
import { activeTrackPointIndex, trackingTimeSeconds } from "./tracking-time";

const track: OverlayTracklet = { id: "track", player_id: null, start_frame: 0, end_frame: 2, team_label: null, side_of_ball: null, position_group: null, track_confidence: null, track_points: [0, 1, 2].map((frame_number, index) => ({ frame_number, timestamp_seconds: [0, 0.05, 0.5][index], bbox: null, field_x: null, field_y: null, detection_confidence: null })) };
describe("tracking presentation times", () => {
  test("variable-rate points follow elapsed video time, including track visibility", () => {
    expect(activeTrackPointIndex(track, 0.46, 30)).toBe(2);
    expect(activeTrackPointIndex(track, 0.04, 30)).toBe(1);
    expect(activeTrackPointIndex(track, 0.8, 30)).toBe(-1);
  });
  test("legacy constant-rate points retain frame-based visibility", () => {
    const legacy = { ...track, track_points: track.track_points.map((point) => ({ ...point, timestamp_seconds: null })) };
    expect(activeTrackPointIndex(legacy, 0.06, 30)).toBe(2);
    expect(activeTrackPointIndex(legacy, 0.46, 30)).toBe(-1);
  });
  test("recording tracks use the source clock for original and rendered clip playback", () => {
    expect(trackingTimeSeconds({ tracks_timebase: "recording", source_start_time_seconds: 125.5 }, 2, 125.5)).toBe(127.5);
    expect(trackingTimeSeconds({ tracks_timebase: "recording" }, 2, 125.5)).toBe(127.5);
    expect(trackingTimeSeconds({ tracks_timebase: "clip" }, 2, 125.5)).toBe(2);
    expect(trackingTimeSeconds(null, 2, 125.5)).toBe(2);
  });
  test("invalid frame rates do not paint an arbitrary position", () => {
    expect(activeTrackPointIndex(track, 0, 0)).toBe(-1);
    expect(activeTrackPointIndex(track, 0, Number.NaN)).toBe(-1);
  });
});
