import type { OverlayTracklet } from "./types";

/** Presentation timestamps are authoritative for variable-frame-rate recordings. */
export function activeTrackPointIndex(track: OverlayTracklet, seconds: number, fps: number): number {
  const points = track.track_points;
  if (!points.length) return -1;
  const time = (index: number) => points[index].timestamp_seconds ?? points[index].frame_number / fps;
  if (points[0].timestamp_seconds != null && points[points.length - 1].timestamp_seconds != null) {
    const first = time(0);
    const last = time(points.length - 1);
    if (seconds < first - 1 / fps || seconds > last + 1 / fps) return -1;
  } else if (seconds * fps < track.start_frame || seconds * fps > track.end_frame) return -1;
  // API points are ordered by source frame; decoded presentation times follow it.
  let lower = 0;
  let upper = points.length - 1;
  while (lower < upper) {
    const middle = Math.floor((lower + upper) / 2);
    if (time(middle) < seconds) lower = middle + 1;
    else upper = middle;
  }
  return lower > 0 && Math.abs(time(lower - 1) - seconds) <= Math.abs(time(lower) - seconds) ? lower - 1 : lower;
}
