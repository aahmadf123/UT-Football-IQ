import type { ClipOverlayPayload, OverlayTracklet, OverlayTrackPoint } from "./types";

/** Tracks are stored against the original recording, even for rendered clips. */
export function trackingTimeSeconds(
  payload: Pick<ClipOverlayPayload, "tracks_timebase" | "source_start_time_seconds"> | null,
  clipLocalSeconds: number,
  clipStartSeconds: number,
): number {
  return clipLocalSeconds + (payload?.tracks_timebase === "recording"
    ? payload.source_start_time_seconds ?? clipStartSeconds
    : 0);
}

/** Presentation timestamps are authoritative for variable-frame-rate recordings. */
export function activeTrackPointIndex(track: OverlayTracklet, seconds: number, fps: number): number {
  return activeTrackSample(track, seconds, fps)?.index ?? -1;
}

export const MAX_INTERPOLATION_GAP_SECONDS = 0.2;
export const MAX_ENDPOINT_FRESHNESS_SECONDS = 0.1;

/** Same-shot, bounded interpolation on the presented frame's source clock. */
export function activeTrackSample(track: OverlayTracklet, seconds: number, fps: number): {
  point: OverlayTrackPoint; index: number; mode: "observed" | "interpolated";
} | null {
  const points = track.track_points;
  if (!points.length || !Number.isFinite(seconds) || !Number.isFinite(fps) || fps <= 0) return null;
  const time = (index: number) => points[index].timestamp_seconds ?? points[index].frame_number / fps;
  // API points are ordered by source frame; decoded presentation times follow it.
  let lower = 0;
  let upper = points.length - 1;
  while (lower < upper) {
    const middle = Math.floor((lower + upper) / 2);
    if (time(middle) <= seconds) lower = middle + 1;
    else upper = middle;
  }
  const before = lower > 0 && time(lower) > seconds ? lower - 1 : lower;
  const after = lower;
  const left = points[before];
  const right = points[after];
  if (!Number.isFinite(time(before)) || !Number.isFinite(time(after))) return null;
  const visible = (point: OverlayTrackPoint) => !["lost", "occluded", "offscreen"].includes(point.visibility_state ?? "");
  const gap = time(after) - time(before);
  const sameShot = (left.shot_id ?? null) === (right.shot_id ?? null);
  if (gap > 0 && gap <= MAX_INTERPOLATION_GAP_SECONDS + 1e-9 && sameShot && visible(left) && visible(right) && seconds > time(before) && seconds < time(after)) {
    const weight = (seconds - time(before)) / gap;
    const blend = (a: number | null, b: number | null) => a != null && b != null ? a + (b - a) * weight : null;
    return { index: weight <= 0.5 ? before : after, mode: "interpolated", point: {
      ...left, timestamp_seconds: seconds,
      bbox: left.bbox && right.bbox ? left.bbox.map((value, i) => value + (right.bbox![i] - value) * weight) as OverlayTrackPoint["bbox"] : null,
      field_x: blend(left.field_x, right.field_x), field_y: blend(left.field_y, right.field_y),
    } };
  }
  // Never look ahead into a different shot or skip an explicit visibility loss.
  const nearest = sameShot && visible(left) && visible(right) && Math.abs(time(after) - seconds) < Math.abs(time(before) - seconds) ? after : before;
  if (!visible(points[nearest]) || Math.abs(time(nearest) - seconds) > MAX_ENDPOINT_FRESHNESS_SECONDS + 1e-9) return null;
  return { point: points[nearest], index: nearest, mode: "observed" };
}
