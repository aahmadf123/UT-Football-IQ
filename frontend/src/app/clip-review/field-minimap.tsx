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
import { activeTrackSample } from "@/lib/tracking-time";

const TEAM_COLORS: Record<string, string> = {
  home: "#fbbf24",
  away: "#38bdf8",
  offense: "#fbbf24",
  defense: "#38bdf8",
  unknown: "#cbd5e1",
};

export function FieldMinimap({
  tracklets,
  currentFrame,
  currentTimeSeconds,
  fps = 30,
  calibration,
  playerNamesById,
  embedded = false,
  quiet = false,
}: {
  tracklets: OverlayTracklet[];
  currentFrame: number;
  currentTimeSeconds?: number;
  fps?: number;
  calibration: OverlayCalibration | null;
  playerNamesById?: ReadonlyMap<string, string>;
  embedded?: boolean;
  quiet?: boolean;
}) {
  if (!calibration || calibration.analytics_safe !== true) {
    if (embedded && quiet) return <div data-testid="field-minimap-gated" className="bg-black/80 px-2 py-1 text-[10px] text-white" title={calibration?.reason ?? "Reliable field calibration is unavailable"}><span aria-hidden>Field unavailable</span><span className="sr-only">{calibration?.reason ?? "Reliable field calibration is unavailable"}</span></div>;
    return (
      <div
        data-testid="field-minimap-gated"
        className={embedded ? "p-2 text-white" : "mt-3 rounded-lg border border-border-soft bg-secondary/30 p-3"}
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
    const point = activeTrackSample(t, currentTimeSeconds ?? currentFrame / fps, fps)?.point;
    if (!point || point.field_x == null || point.field_y == null || !Number.isFinite(point.field_x) || !Number.isFinite(point.field_y)) continue;
    const name = t.player_id != null ? playerNamesById?.get(t.player_id) : undefined;
    if (point.field_x < -10 || point.field_x > 110 || Math.abs(point.field_y) > 53.3334 / 2) continue;
    markers.push({
      // FieldDiagram marker x is 0–100 between the goal lines (it applies the
      // end-zone offset itself) — same span as the pipeline's field template.
      // The pipeline's field_y is centered (±26.665 yd about the midline);
      // shift to the diagram's 0–53.3 span.
      x: point.field_x,
      y: point.field_y + (160 / 3) / 2,
      label: quiet ? undefined : embedded ? name?.match(/^#\S+/)?.[0] ?? `T${i + 1}` : name ?? `T${i + 1}`,
      color: TEAM_COLORS[t.team_label ?? "unknown"] ?? TEAM_COLORS.unknown,
    });
  }

  return (
    <div data-testid="field-minimap" className={embedded ? "" : "mt-3"}>
      {!embedded && <p className="m-0 mb-1.5 text-xs font-semibold uppercase tracking-widest text-muted-foreground">
        Field view
      </p>}
      {markers.length > 0 || embedded ? (
        <><FieldDiagram markers={markers} compact={embedded} quiet={quiet} />{embedded ? <p className={quiet ? "sr-only" : "px-2 py-1 text-[10px] text-white/85"}>{markers.length} visible track{markers.length === 1 ? "" : "s"} · calibrated yards</p> : null}</>
      ) : (
        <p className="text-xs text-muted-foreground">
          No calibrated player positions at this moment of the clip.
        </p>
      )}
    </div>
  );
}
