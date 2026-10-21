import { describe, expect, test } from "vitest";
import type { OverlayTracklet } from "./types";
import { activeTrackPointIndex, activeTrackSample, trackingTimeSeconds } from "./tracking-time";

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
  test("long observation gaps never paint a stuck player", () => {
    const sparse = { ...track, end_frame: 300, track_points: [track.track_points[0], { ...track.track_points[2], frame_number: 300, timestamp_seconds: 10 }] };
    expect(activeTrackPointIndex(sparse, 5, 30)).toBe(-1);
    expect(activeTrackSample(sparse, 5, 30)).toBeNull();
  });
  test("interpolation is bounded and records its evidence state", () => {
    const dense = { ...track, track_points: [0, 0.2].map((timestamp_seconds, i) => ({ ...track.track_points[0], timestamp_seconds, frame_number: i * 6, bbox: [i * 20, 0, 10 + i * 20, 20] as [number, number, number, number], field_x: i * 2 })) };
    expect(activeTrackSample(dense, 0.1, 30)).toMatchObject({ mode: "interpolated", point: { bbox: [10, 0, 20, 20], field_x: 1 } });
    expect(activeTrackSample(dense, 0.31, 30)).toBeNull();
    expect(activeTrackSample(dense, 0.29, 30)?.mode).toBe("observed");
  });
  test("lost tracks and shot cuts cannot be interpolated", () => {
    const cut = { ...track, track_points: [ { ...track.track_points[0], timestamp_seconds: 0, shot_id: "a" }, { ...track.track_points[1], timestamp_seconds: 0.2, shot_id: "b" } ] };
    expect(activeTrackSample(cut, 0.15, 30)).toBeNull();
    expect(activeTrackSample({ ...cut, track_points: cut.track_points.map((p) => ({ ...p, visibility_state: "lost" })) }, 0, 30)).toBeNull();
  });
  test("malformed source times hide markers and latest same-time loss wins", () => {
    expect(activeTrackSample({ ...track, track_points: [{ ...track.track_points[0], timestamp_seconds: Number.NaN }] }, 0, 30)).toBeNull();
    expect(activeTrackSample({ ...track, track_points: [track.track_points[0], { ...track.track_points[0], visibility_state: "lost" }] }, 0, 30)).toBeNull();
  });
});
