"use client";

/**
 * SVG overlay layer rendered on top of the clip-review video.
 *
 * The canvas takes the overlay payload + the current playback time (in
 * seconds) and draws, per visible tracklet:
 *   - the detection bounding box + a recent path tail (tracks layer)
 *   - an identity label — roster name/jersey when the tracklet is attributed,
 *     plus the tracklet's own confidence (labels layer)
 *   - a per-player speed callout where a coach-visible metric exists
 *     (metrics layer)
 *   - active event badges within a small window (events layer)
 *
 * Space mapping: everything on the camera layer comes from detection bboxes
 * in video pixel space. The SVG viewBox matches the video's intrinsic size
 * with `preserveAspectRatio="xMidYMid meet"` so boxes stay glued to pixels
 * through letterboxing. Field coordinates (`field_x/field_y`) are deliberately
 * NOT painted here — a top-down projection does not land on players in a
 * camera image; they live in the Field view mini-map instead.
 *
 * Interactivity: the SVG itself ignores the pointer, but each tracklet group
 * is clickable — clicking a player selects that tracklet and prefills the
 * corrections panel.
 */

import type { OverlayEvent, OverlayLayerKey, OverlayTracklet } from "@/lib/types";

// Window (seconds) around currentTime in which an event is considered active.
const EVENT_ACTIVE_WINDOW_S = 0.4;
// Length of the trailing path drawn behind each player marker (frames).
const TRAIL_FRAMES = 30;
// Fallback intrinsic size when the video row has no dimensions recorded.
const DEFAULT_VIEW_W = 1280;
const DEFAULT_VIEW_H = 720;

// Team colors resolve from the design tokens at render time (the canvas is
// inline SVG in the DOM, so var() works): home = Toledo gold, away = info
// blue, unknown = muted ink.
const TEAM_COLORS: Record<string, string> = {
  home: "var(--primary)",
  away: "var(--status-info)",
  unknown: "var(--muted-foreground)",
};

interface OverlayCanvasProps {
  tracklets: OverlayTracklet[];
  events: OverlayEvent[];
  currentTimeSeconds: number;
  fps: number;
  videoWidth: number | null;
  videoHeight: number | null;
  activeLayers: ReadonlySet<OverlayLayerKey>;
  /** Roster display names ("#7 C. Jones") keyed by player id, for labels. */
  playerNamesById?: ReadonlyMap<string, string>;
  /** Coach-visible speed callout text keyed by tracklet id, for metrics. */
  metricTextByTrackletId?: ReadonlyMap<string, string>;
  selectedTrackletId?: string | null;
  onSelectTracklet?: (trackletId: string) => void;
}

export function OverlayCanvas({
  tracklets,
  events,
  currentTimeSeconds,
  fps,
  videoWidth,
  videoHeight,
  activeLayers,
  playerNamesById,
  metricTextByTrackletId,
  selectedTrackletId,
  onSelectTracklet,
}: OverlayCanvasProps) {
  const showTracks = activeLayers.has("tracks");
  const showEvents = activeLayers.has("events");
  const showLabels = activeLayers.has("labels");
  const showMetrics = activeLayers.has("metrics");

  // Labels/metrics stand alone: glyphs render (without boxes) when tracks are
  // off, so those toggles aren't dead. Nothing on → no canvas at all.
  if (!showTracks && !showLabels && !showMetrics && !showEvents) return null;

  const currentFrame = Math.round(currentTimeSeconds * fps);
  const viewW = videoWidth ?? DEFAULT_VIEW_W;
  const viewH = videoHeight ?? DEFAULT_VIEW_H;

  return (
    <svg
      data-testid="clip-overlay-svg"
      role="group"
      aria-label="Tracking overlays"
      viewBox={`0 0 ${viewW} ${viewH}`}
      preserveAspectRatio="xMidYMid meet"
      style={{
        position: "absolute",
        inset: 0,
        width: "100%",
        height: "100%",
        // The SVG surface stays transparent to the pointer so the video's
        // click-to-pause keeps working; tracklet groups opt back in.
        pointerEvents: "none",
      }}
    >
      {(showTracks || showLabels || showMetrics) &&
        tracklets.map((t, i) => (
          <TrackletGlyph
            key={t.id}
            tracklet={t}
            index={i}
            currentFrame={currentFrame}
            viewW={viewW}
            viewH={viewH}
            videoWidth={videoWidth}
            videoHeight={videoHeight}
            showBox={showTracks}
            showLabel={showLabels}
            metricText={showMetrics ? metricTextByTrackletId?.get(t.id) : undefined}
            playerName={
              t.player_id != null ? playerNamesById?.get(t.player_id) : undefined
            }
            selected={selectedTrackletId === t.id}
            onSelect={onSelectTracklet}
          />
        ))}
      {showEvents && (
        <ActiveEventBadges
          events={events}
          currentTimeSeconds={currentTimeSeconds}
          fps={fps}
          viewW={viewW}
          viewH={viewH}
        />
      )}
    </svg>
  );
}

function TrackletGlyph({
  tracklet,
  index,
  currentFrame,
  viewW,
  viewH,
  videoWidth,
  videoHeight,
  showBox,
  showLabel,
  metricText,
  playerName,
  selected,
  onSelect,
}: {
  tracklet: OverlayTracklet;
  // Position of the tracklet in the payload order — rendered as the ``T{n}``
  // tag next to the marker so a coach can reference the same track in the
  // corrections panel.
  index: number;
  currentFrame: number;
  viewW: number;
  viewH: number;
  videoWidth: number | null;
  videoHeight: number | null;
  /** Tracks layer on — draw the bounding box + trail. */
  showBox: boolean;
  showLabel: boolean;
  metricText: string | undefined;
  playerName: string | undefined;
  selected: boolean;
  onSelect?: (trackletId: string) => void;
}) {
  if (
    currentFrame < tracklet.start_frame ||
    currentFrame > tracklet.end_frame ||
    tracklet.track_points.length === 0
  ) {
    return null;
  }

  // Nearest-neighbor lookup. Points are inserted in order but no guarantee
  // every frame has a sample, so scan.
  let activeIdx = -1;
  let bestDistance = Number.POSITIVE_INFINITY;
  for (let i = 0; i < tracklet.track_points.length; i++) {
    const d = Math.abs(tracklet.track_points[i].frame_number - currentFrame);
    if (d < bestDistance) {
      bestDistance = d;
      activeIdx = i;
    }
  }
  if (activeIdx < 0) return null;

  const activePoint = tracklet.track_points[activeIdx];
  const box = projectBox(activePoint, videoWidth, videoHeight, viewW, viewH);
  if (!box) return null;

  const color = TEAM_COLORS[tracklet.team_label ?? "unknown"] ?? TEAM_COLORS.unknown;

  // Build a trailing path from the last TRAIL_FRAMES samples (box centers).
  const trailStart = Math.max(0, activeIdx - TRAIL_FRAMES);
  const trail: string[] = [];
  for (let i = trailStart; i <= activeIdx; i++) {
    const p = projectBox(tracklet.track_points[i], videoWidth, videoHeight, viewW, viewH);
    if (!p) continue;
    trail.push(`${trail.length === 0 ? "M" : "L"} ${p.cx} ${p.cy}`);
  }

  const tag = `T${index + 1}`;
  const identity = playerName ?? tag;
  const confidencePct =
    tracklet.track_confidence != null ? `${Math.round(tracklet.track_confidence * 100)}%` : null;
  const labelText = confidencePct ? `${identity} · ${confidencePct}` : identity;
  // Scale strokes/text with the intrinsic resolution so a 4K video doesn't
  // render hairline boxes.
  const scale = viewW / 1000;

  return (
    <g
      data-testid={`overlay-tracklet-${tracklet.id}`}
      role="button"
      aria-label={`Select ${identity}${confidencePct ? `, tracking confidence ${confidencePct}` : ""}`}
      tabIndex={0}
      onClick={(e) => {
        e.stopPropagation();
        onSelect?.(tracklet.id);
      }}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onSelect?.(tracklet.id);
        }
      }}
      style={{ pointerEvents: "auto", cursor: onSelect ? "pointer" : "default" }}
    >
      {showBox && trail.length > 1 && (
        <path
          d={trail.join(" ")}
          stroke={color}
          strokeWidth={2 * scale}
          fill="none"
          opacity={0.6}
        />
      )}
      {showBox && (
        <rect
          x={box.x}
          y={box.y}
          width={box.w}
          height={box.h}
          fill={selected ? color : "transparent"}
          fillOpacity={selected ? 0.15 : 0}
          stroke={color}
          strokeWidth={(selected ? 3 : 1.8) * scale}
          rx={2 * scale}
        />
      )}
      {(showLabel || showBox) && (
        <text
          data-testid={`overlay-tracklet-tag-${tracklet.id}`}
          x={box.x}
          y={Math.max(box.y - 6 * scale, 12 * scale)}
          fontSize={(showLabel ? 13 : 11) * scale}
          fontFamily="ui-sans-serif"
          fontWeight={700}
          fill={color}
          stroke="rgba(15,23,42,0.85)"
          strokeWidth={0.6 * scale}
          paintOrder="stroke"
        >
          {showLabel ? labelText : tag}
        </text>
      )}
      {metricText != null && (
        <text
          data-testid={`overlay-tracklet-metric-${tracklet.id}`}
          x={box.x}
          y={box.y + box.h + 14 * scale}
          fontSize={11 * scale}
          fontFamily="var(--font-mono, ui-monospace)"
          fill="var(--foreground, #fff)"
          stroke="rgba(15,23,42,0.85)"
          strokeWidth={0.6 * scale}
          paintOrder="stroke"
        >
          {metricText}
        </text>
      )}
    </g>
  );
}

/**
 * Project a track point's bbox into viewBox space. Camera overlays are drawn
 * ONLY from bboxes — field coordinates belong to the mini-map, never painted
 * over the camera image.
 */
function projectBox(
  point: { bbox: [number, number, number, number] | null },
  videoWidth: number | null,
  videoHeight: number | null,
  viewW: number,
  viewH: number,
): { x: number; y: number; w: number; h: number; cx: number; cy: number } | null {
  if (!point.bbox) return null;
  const [x1, y1, x2, y2] = point.bbox;
  // When the video's intrinsic size is known the viewBox equals it and this
  // is an identity mapping; otherwise scale into the fallback box.
  const sx = videoWidth ? viewW / videoWidth : 1;
  const sy = videoHeight ? viewH / videoHeight : 1;
  const x = x1 * sx;
  const y = y1 * sy;
  const w = Math.max((x2 - x1) * sx, 2);
  const h = Math.max((y2 - y1) * sy, 2);
  return { x, y, w, h, cx: x + w / 2, cy: y + h / 2 };
}

function ActiveEventBadges({
  events,
  currentTimeSeconds,
  fps,
  viewW,
  viewH,
}: {
  events: OverlayEvent[];
  currentTimeSeconds: number;
  fps: number;
  viewW: number;
  viewH: number;
}) {
  const active = events.filter((e) => {
    const t = eventTimeSeconds(e, fps);
    if (t == null) return false;
    return Math.abs(t - currentTimeSeconds) <= EVENT_ACTIVE_WINDOW_S;
  });
  if (active.length === 0) return null;
  const scale = viewW / 1000;
  void viewH;
  return (
    <g data-testid="overlay-active-events">
      {active.map((e, i) => (
        <g key={e.id} transform={`translate(${40 * scale} ${(80 + i * 28) * scale})`}>
          <rect
            x={0}
            y={0}
            rx={4 * scale}
            ry={4 * scale}
            width={140 * scale}
            height={22 * scale}
            fill="rgba(15,23,42,0.85)"
          />
          <text
            x={8 * scale}
            y={15 * scale}
            fontSize={12 * scale}
            fill="var(--primary)"
            fontFamily="var(--font-mono, ui-monospace)"
          >
            {e.event_type}
          </text>
        </g>
      ))}
    </g>
  );
}

export function eventTimeSeconds(e: OverlayEvent, fps: number): number | null {
  if (e.timestamp_seconds != null) return e.timestamp_seconds;
  if (e.frame_number != null && fps > 0) return e.frame_number / fps;
  return null;
}
