"use client";

/**
 * SVG overlay layer rendered on top of the clip-review video.
 *
 * The canvas takes the overlay payload + the current playback time (in
 * seconds) and draws:
 *   - per-player markers + recent path tails (tracks layer)
 *   - active event markers within a small window (events layer)
 *   - a field wireframe outline (wireframe layer)
 *
 * Time/space mapping:
 *   - Tracklets carry frame numbers. The canvas converts ``currentTime``
 *     seconds to a frame index using ``fps`` (defaults to 30 — Phase CV
 *     pipeline default), then picks the nearest-neighbor sample per
 *     tracklet. This is intentionally coarse for the P1 overlay shell;
 *     richer interpolation lands with B11.
 *   - ``field_x`` / ``field_y`` are in yards (standard 53.33 × 120 field).
 *     Normalized into 0..1 then projected onto the SVG box.
 *   - Tracklets whose points have no field coordinates (e.g. calibration
 *     below threshold) fall back to bounding-box centers in pixel space,
 *     normalized against the video's intrinsic dimensions when provided.
 */

import type { OverlayEvent, OverlayLayerKey, OverlayTracklet } from "@/lib/types";

const FIELD_WIDTH_YARDS = 53.33;
const FIELD_LENGTH_YARDS = 120;
// Window (seconds) around currentTime in which an event is considered active.
const EVENT_ACTIVE_WINDOW_S = 0.4;
// Length of the trailing path drawn behind each player marker (frames).
const TRAIL_FRAMES = 30;

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
}

export function OverlayCanvas({
  tracklets,
  events,
  currentTimeSeconds,
  fps,
  videoWidth,
  videoHeight,
  activeLayers,
}: OverlayCanvasProps) {
  const showTracks = activeLayers.has("tracks");
  const showEvents = activeLayers.has("events");
  const showWireframe = activeLayers.has("wireframe");

  if (!showTracks && !showEvents && !showWireframe) return null;

  const currentFrame = Math.round(currentTimeSeconds * fps);

  return (
    <svg
      data-testid="clip-overlay-svg"
      aria-hidden="true"
      viewBox="0 0 1000 600"
      preserveAspectRatio="none"
      style={{
        position: "absolute",
        inset: 0,
        width: "100%",
        height: "100%",
        pointerEvents: "none",
      }}
    >
      {showWireframe && <FieldWireframe />}
      {showTracks &&
        tracklets.map((t, i) => (
          <TrackletGlyph
            key={t.id}
            tracklet={t}
            index={i}
            currentFrame={currentFrame}
            videoWidth={videoWidth}
            videoHeight={videoHeight}
          />
        ))}
      {showEvents && (
        <ActiveEventBadges
          events={events}
          currentTimeSeconds={currentTimeSeconds}
          fps={fps}
        />
      )}
    </svg>
  );
}

function FieldWireframe() {
  // Light hashmarks/sideline outline rendered in viewBox coordinates so it
  // scales with the SVG. Stroke is subtle so it doesn't fight the video.
  return (
    <g
      data-testid="overlay-wireframe"
      stroke="rgba(251,191,36,0.35)"
      strokeWidth={1}
      fill="none"
    >
      <rect x={20} y={40} width={960} height={520} />
      {Array.from({ length: 11 }).map((_, i) => {
        const x = 20 + (960 * i) / 10;
        return <line key={i} x1={x} y1={40} x2={x} y2={560} />;
      })}
    </g>
  );
}

function TrackletGlyph({
  tracklet,
  index,
  currentFrame,
  videoWidth,
  videoHeight,
}: {
  tracklet: OverlayTracklet;
  // Position of the tracklet in the payload order — rendered as the ``T{n}``
  // tag next to the marker so a coach can reference the same track in the
  // corrections panel.
  index: number;
  currentFrame: number;
  videoWidth: number | null;
  videoHeight: number | null;
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
  const activeProjected = projectPoint(activePoint, videoWidth, videoHeight);
  if (!activeProjected) return null;

  const color = TEAM_COLORS[tracklet.team_label ?? "unknown"] ?? TEAM_COLORS.unknown;

  // Build a trailing path from the last TRAIL_FRAMES samples.
  const trailStart = Math.max(0, activeIdx - TRAIL_FRAMES);
  const trail: string[] = [];
  for (let i = trailStart; i <= activeIdx; i++) {
    const projected = projectPoint(tracklet.track_points[i], videoWidth, videoHeight);
    if (!projected) continue;
    trail.push(`${trail.length === 0 ? "M" : "L"} ${projected.x} ${projected.y}`);
  }

  return (
    <g data-testid={`overlay-tracklet-${tracklet.id}`}>
      {trail.length > 1 && (
        <path d={trail.join(" ")} stroke={color} strokeWidth={2} fill="none" opacity={0.6} />
      )}
      <circle cx={activeProjected.x} cy={activeProjected.y} r={8} fill={color} opacity={0.85} />
      <text
        data-testid={`overlay-tracklet-tag-${tracklet.id}`}
        x={activeProjected.x + 11}
        y={activeProjected.y - 9}
        fontSize={11}
        fontFamily="ui-sans-serif"
        fontWeight={700}
        fill={color}
        stroke="rgba(15,23,42,0.85)"
        strokeWidth={0.6}
        paintOrder="stroke"
      >
        {`T${index + 1}`}
      </text>
    </g>
  );
}

function projectPoint(
  point: { field_x: number | null; field_y: number | null; bbox: [number, number, number, number] | null },
  videoWidth: number | null,
  videoHeight: number | null,
): { x: number; y: number } | null {
  if (point.field_x != null && point.field_y != null) {
    const nx = clamp01(point.field_x / FIELD_LENGTH_YARDS);
    const ny = clamp01(point.field_y / FIELD_WIDTH_YARDS);
    return { x: nx * 1000, y: ny * 600 };
  }
  if (point.bbox && videoWidth && videoHeight) {
    const cx = (point.bbox[0] + point.bbox[2]) / 2;
    const cy = (point.bbox[1] + point.bbox[3]) / 2;
    return { x: (cx / videoWidth) * 1000, y: (cy / videoHeight) * 600 };
  }
  return null;
}

function clamp01(n: number): number {
  if (n < 0) return 0;
  if (n > 1) return 1;
  return n;
}

function ActiveEventBadges({
  events,
  currentTimeSeconds,
  fps,
}: {
  events: OverlayEvent[];
  currentTimeSeconds: number;
  fps: number;
}) {
  const active = events.filter((e) => {
    const t = eventTimeSeconds(e, fps);
    if (t == null) return false;
    return Math.abs(t - currentTimeSeconds) <= EVENT_ACTIVE_WINDOW_S;
  });
  if (active.length === 0) return null;
  return (
    <g data-testid="overlay-active-events">
      {active.map((e, i) => (
        <g key={e.id} transform={`translate(40 ${80 + i * 28})`}>
          <rect x={0} y={0} rx={4} ry={4} width={140} height={22} fill="rgba(15,23,42,0.85)" />
          <text x={8} y={15} fontSize={12} fill="var(--primary)" fontFamily="var(--font-mono, ui-monospace)">
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
