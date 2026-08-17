"use client";

/**
 * Top-down field view for Clip Review.
 *
 * Plots each tracklet's calibrated field position (`field_x`/`field_y`, in
 * yards) at the current frame on the NCAA field schematic. Rendered ONLY when
 * the clip's calibration is `analytics_safe` — projecting uncalibrated
 * coordinates would be fabrication, so below the gate the panel explains
 * itself with the backend's coach-readable reason instead.
 */

import { FieldDiagram, type FieldMarker } from "@/components/field-diagram";
import type { OverlayCalibration, OverlayTracklet } from "@/lib/types";

const TEAM_COLORS: Record<string, string> = {
  home: "#fbbf24",
  away: "#38bdf8",
  unknown: "#cbd5e1",
};

export function FieldMinimap({
  tracklets,
  currentFrame,
  calibration,
  playerNamesById,
}: {
  tracklets: OverlayTracklet[];
  currentFrame: number;
  calibration: OverlayCalibration | null;
  playerNamesById?: ReadonlyMap<string, string>;
}) {
  if (!calibration || calibration.analytics_safe !== true) {
    return (
      <div
        data-testid="field-minimap-gated"
        className="mt-3 rounded-lg border border-border-soft bg-secondary/30 p-3"
      >
        <p className="m-0 text-xs font-semibold uppercase tracking-widest text-muted-foreground">
          Field view
        </p>
        <p className="mt-1 text-xs text-muted-foreground">
          {calibration?.reason ??
            "Unavailable — the field could not be calibrated for this footage, so top-down positions can't be trusted."}
        </p>
      </div>
    );
  }

  const markers: FieldMarker[] = [];
  for (let i = 0; i < tracklets.length; i++) {
    const t = tracklets[i];
    if (currentFrame < t.start_frame || currentFrame > t.end_frame) continue;
    // Nearest sample at the current frame with usable field coordinates.
    let best: { d: number; x: number; y: number } | null = null;
    for (const p of t.track_points) {
      if (p.field_x == null || p.field_y == null) continue;
      const d = Math.abs(p.frame_number - currentFrame);
      if (best == null || d < best.d) best = { d, x: p.field_x, y: p.field_y };
    }
    if (!best) continue;
    const name = t.player_id != null ? playerNamesById?.get(t.player_id) : undefined;
    markers.push({
      // FieldDiagram marker x is 0–100 between the goal lines (it applies the
      // end-zone offset itself) — same span as the pipeline's field template.
      // The pipeline's field_y is centered (±26.665 yd about the midline);
      // shift to the diagram's 0–53.3 span.
      x: best.x,
      y: best.y + 53.3 / 2,
      label: name ?? `T${i + 1}`,
      color: TEAM_COLORS[t.team_label ?? "unknown"] ?? TEAM_COLORS.unknown,
    });
  }

  return (
    <div data-testid="field-minimap" className="mt-3">
      <p className="m-0 mb-1.5 text-xs font-semibold uppercase tracking-widest text-muted-foreground">
        Field view
      </p>
      {markers.length > 0 ? (
        <FieldDiagram markers={markers} />
      ) : (
        <p className="text-xs text-muted-foreground">
          No calibrated player positions at this moment of the clip.
        </p>
      )}
    </div>
  );
}
