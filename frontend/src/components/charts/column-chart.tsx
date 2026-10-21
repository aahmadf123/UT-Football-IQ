"use client";

/**
 * Columns from a zero baseline (values may be negative), one per item.
 *
 * Columns are ≤ 24px thick with a 4px rounded data-end, square at the
 * baseline, and a 2px surface gap between neighbours. Each column's full
 * height is its hit target: hover or focus shows its tooltip, Enter or a
 * click opens it. Optional dividers mark sections (quarters); optional
 * markers sit above a column (a touchdown, a turnover).
 */

import { useState, type ReactNode } from "react";
import { niceTicks, useWidth } from "./use-width";

export interface Column {
  key: string;
  value: number;
  color: string;
  /** Tooltip lines; the first is the strong value line. */
  tooltip: string[];
  /** Tick label under the column (histograms); omit for dense series. */
  tick?: string;
  /** A small glyph above the column, e.g. "TD" or "TO". */
  marker?: string;
  /** Value label at the column's cap (only drawn when it fits). */
  capLabel?: string;
}

interface ColumnChartProps {
  columns: Column[];
  ariaLabel: string;
  formatY: (value: number) => string;
  height?: number;
  dividers?: { before: number; label: string }[];
  onColumnClick?: (index: number) => void;
  legend?: ReactNode;
}

const PAD = { top: 18, right: 8, bottom: 24, left: 40 };

export function ColumnChart({ columns, ariaLabel, formatY, height = 220, dividers = [], onColumnClick, legend }: ColumnChartProps) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const [active, setActive] = useState<number | null>(null);
  if (!columns.length) return null;

  const values = columns.map((c) => c.value);
  const ticks = niceTicks(Math.min(0, ...values), Math.max(0, ...values));
  const yMin = ticks[0];
  const yMax = ticks[ticks.length - 1];
  const plotW = Math.max(40, width - PAD.left - PAD.right);
  const plotH = height - PAD.top - PAD.bottom;
  const slot = plotW / columns.length;
  const barW = Math.max(1, Math.min(24, slot - 2));
  const yFor = (v: number) => PAD.top + plotH - ((v - yMin) / (yMax - yMin || 1)) * plotH;
  const zero = yFor(0);
  const xFor = (i: number) => PAD.left + i * slot + (slot - barW) / 2;
  const radius = Math.min(4, barW / 2);

  const bar = (i: number, value: number) => {
    const top = Math.min(yFor(value), zero);
    const h = Math.max(1, Math.abs(yFor(value) - zero));
    const x = xFor(i);
    const r = Math.min(radius, h);
    // Rounded at the data end only; square on the baseline.
    if (value >= 0) {
      return `M${x},${zero} V${top + r} Q${x},${top} ${x + r},${top} H${x + barW - r} Q${x + barW},${top} ${x + barW},${top + r} V${zero} Z`;
    }
    const bottom = top + h;
    return `M${x},${zero} V${bottom - r} Q${x},${bottom} ${x + r},${bottom} H${x + barW - r} Q${x + barW},${bottom} ${x + barW},${bottom - r} V${zero} Z`;
  };

  const activeColumn = active != null ? columns[active] : null;
  const tipLeft = active != null ? Math.min(Math.max(xFor(active) + barW + 8, 0), width - 220) : 0;

  return (
    <div ref={ref} className="relative w-full">
      <svg role="img" aria-label={ariaLabel} width={width} height={height} className="block" onPointerLeave={() => setActive(null)}>
        {ticks.map((tick) => (
          <g key={tick}>
            <line x1={PAD.left} x2={PAD.left + plotW} y1={yFor(tick)} y2={yFor(tick)} stroke={tick === 0 ? "var(--muted-foreground)" : "var(--border-soft)"} strokeWidth={1} opacity={tick === 0 ? 0.6 : 1} />
            <text x={PAD.left - 6} y={yFor(tick)} dy="0.32em" textAnchor="end" fontSize={11} fill="var(--muted-foreground)" style={{ fontVariantNumeric: "tabular-nums" }}>
              {formatY(tick)}
            </text>
          </g>
        ))}
        {dividers.map((d) => (
          <g key={d.before}>
            {d.before > 0 && <line x1={PAD.left + d.before * slot} x2={PAD.left + d.before * slot} y1={PAD.top - 14} y2={PAD.top + plotH} stroke="var(--border)" strokeWidth={1} />}
            <text x={PAD.left + d.before * slot + 4} y={PAD.top - 6} fontSize={10} fill="var(--muted-foreground)">{d.label}</text>
          </g>
        ))}
        {columns.map((c, i) => (
          <g key={c.key}>
            <path d={bar(i, c.value)} fill={c.color} opacity={active == null || active === i ? 1 : 0.55} />
            {c.marker && (
              <text x={xFor(i) + barW / 2} y={Math.min(yFor(Math.max(c.value, 0)), zero) - 4} textAnchor="middle" fontSize={9} fontWeight={700} fill="var(--foreground)">
                {c.marker}
              </text>
            )}
            {c.capLabel && barW >= 18 && (
              <text x={xFor(i) + barW / 2} y={yFor(c.value) - (c.value >= 0 ? 4 : -12)} textAnchor="middle" fontSize={11} fill="var(--foreground)" style={{ fontVariantNumeric: "tabular-nums" }}>
                {c.capLabel}
              </text>
            )}
            {c.tick && (
              <text x={xFor(i) + barW / 2} y={height - 8} textAnchor="middle" fontSize={11} fill="var(--muted-foreground)">{c.tick}</text>
            )}
            {/* The whole column height is the target, not the painted pixels. */}
            <rect
              x={PAD.left + i * slot}
              y={PAD.top}
              width={slot}
              height={plotH}
              fill="transparent"
              tabIndex={0}
              role="button"
              aria-label={c.tooltip.join(", ")}
              style={{ cursor: onColumnClick ? "pointer" : "default", outline: "none" }}
              onPointerEnter={() => setActive(i)}
              onFocus={() => setActive(i)}
              onBlur={() => setActive(null)}
              onClick={() => onColumnClick?.(i)}
              onKeyDown={(e) => {
                if ((e.key === "Enter" || e.key === " ") && onColumnClick) {
                  e.preventDefault();
                  onColumnClick(i);
                }
              }}
            />
          </g>
        ))}
        {active != null && (
          <rect x={PAD.left + active * slot} y={PAD.top} width={slot} height={plotH} fill="var(--foreground)" opacity={0.05} pointerEvents="none" />
        )}
      </svg>
      {activeColumn && (
        <div role="status" className="pointer-events-none absolute top-0 z-10 max-w-56 rounded-md border border-border bg-popover px-2.5 py-2 text-xs shadow-lg" style={{ left: tipLeft }}>
          {activeColumn.tooltip.map((line, i) => (
            <p key={i} className={i === 0 ? "font-semibold text-foreground" : "text-muted-foreground"}>{line}</p>
          ))}
        </div>
      )}
      {legend}
    </div>
  );
}
