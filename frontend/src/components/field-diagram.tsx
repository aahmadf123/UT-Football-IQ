"use client";

/**
 * FieldDiagram — a small, dependency-free NCAA football field rendered as SVG.
 *
 * This is the working sample backing the Issue #169 visualization spike: it
 * demonstrates that an interactive football field can be drawn with native SVG
 * without adding sportypy (Python) or cfbplotR / sportyR (R) as runtime
 * dependencies. See docs/adr/0002-field-visualization.md for the decision.
 *
 * It is a generic field schematic (yard lines + hash marks), not a depiction of
 * any specific tracked play, so it never presents data as real (Issue #96).
 * Pass ``markers`` to overlay calibrated coordinates once a tracking pipeline
 * provides them.
 */

export interface FieldMarker {
  /** 0–100 along the length of the field (goal line to goal line). */
  x: number;
  /** 0–53.3 across the width of the field (yards). */
  y: number;
  label?: string;
  color?: string;
}

const FIELD_LENGTH = 120; // 100 yards + two 10-yard end zones
const FIELD_WIDTH = 160 / 3; // NCAA field width in yards
const PX_PER_YARD = 8;

export function FieldDiagram({
  markers = [],
  className,
  compact = false,
  quiet = false,
}: {
  markers?: FieldMarker[];
  className?: string;
  compact?: boolean;
  quiet?: boolean;
}) {
  const w = FIELD_LENGTH * PX_PER_YARD;
  const h = FIELD_WIDTH * PX_PER_YARD;
  // Yard lines every 5 yards between the goal lines (10..110 in field coords).
  const yardLines = Array.from({ length: 21 }, (_, i) => 10 + i * 5);

  return (
    <svg
      viewBox={`0 0 ${w} ${h}`}
      className={className}
      role="img"
      aria-label="NCAA football field schematic"
      data-testid="field-diagram"
      style={{ width: "100%", height: "auto", display: "block", borderRadius: 8 }}
    >
      {/* Turf */}
      <rect x={0} y={0} width={w} height={h} fill="#1f6b3a" opacity={quiet ? 0.22 : 1} />
      {/* End zones */}
      <rect x={0} y={0} width={10 * PX_PER_YARD} height={h} fill="#15397f" opacity={quiet ? 0.2 : 0.85} />
      <rect
        x={110 * PX_PER_YARD}
        y={0}
        width={10 * PX_PER_YARD}
        height={h}
        fill="#15397f"
        opacity={quiet ? 0.2 : 0.85}
      />
      {/* Yard lines */}
      {yardLines.map((yard) => {
        const x = yard * PX_PER_YARD;
        const isGoal = yard === 10 || yard === 110;
        return (
          <line
            key={yard}
            x1={x}
            y1={0}
            x2={x}
            y2={h}
            stroke="#ffffff"
            strokeOpacity={quiet ? isGoal ? 0.5 : 0.23 : isGoal ? 0.95 : 0.55}
            strokeWidth={isGoal ? 2 : 1}
          />
        );
      })}
      {/* Hash marks (NCAA hashes ~60 ft / 20 yds from each sideline). */}
      {yardLines.map((yard) => {
        const x = yard * PX_PER_YARD;
        return (
          <g key={`hash-${yard}`} stroke="#ffffff" strokeOpacity={quiet ? 0.24 : 0.7} strokeWidth={1}>
            <line x1={x - 3} y1={20 * PX_PER_YARD} x2={x + 3} y2={20 * PX_PER_YARD} />
            <line x1={x - 3} y1={(FIELD_WIDTH - 20) * PX_PER_YARD} x2={x + 3} y2={(FIELD_WIDTH - 20) * PX_PER_YARD} />
          </g>
        );
      })}
      {/* Optional calibrated markers */}
      {markers.map((m, i) => (
        <g key={`marker-${i}`}>
          <circle
            cx={(m.x / 100) * (100 * PX_PER_YARD) + 10 * PX_PER_YARD}
            cy={(m.y / FIELD_WIDTH) * h}
            r={compact ? 8 : 5}
            fill={m.color ?? "#ffd20a"}
            stroke="#0b1c3f"
            strokeWidth={1}
          />
          {m.label && (
            <text
              x={(m.x / 100) * (100 * PX_PER_YARD) + 10 * PX_PER_YARD}
              y={(m.y / FIELD_WIDTH) * h - 8}
              fill="#ffffff"
              fontSize={compact ? 20 : 11}
              textAnchor="middle"
            >
              {m.label}
            </text>
          )}
        </g>
      ))}
    </svg>
  );
}
