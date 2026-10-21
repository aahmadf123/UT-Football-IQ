"use client";

/**
 * Multi-series line chart over time (SVG, no chart library).
 *
 * One y-axis, 2px lines, ≥8px end markers with a surface ring, hairline
 * solid grid. A crosshair snaps to the nearest date and the tooltip lists
 * every series there; arrow keys move it for keyboard users. Two or more
 * series get a legend, and end labels when they don't collide. Every chart
 * carries a table view, so no value is reachable only by hovering.
 */

import { useId, useMemo, useState, type KeyboardEvent, type PointerEvent } from "react";
import { niceTicks, useWidth } from "./use-width";

export interface LinePoint {
  /** Milliseconds since epoch; the x position. */
  x: number;
  y: number | null;
}

export interface LineSeries {
  key: string;
  label: string;
  color: string;
  points: LinePoint[];
}

interface LineChartProps {
  series: LineSeries[];
  ariaLabel: string;
  formatY: (value: number) => string;
  formatX?: (x: number) => string;
  height?: number;
  /** Clicking a point (e.g. a game) opens its detail. */
  onPointClick?: (seriesKey: string, x: number) => void;
  /** Ticks always include zero (counts, rates). */
  zeroBaseline?: boolean;
}

const PAD = { top: 12, right: 16, bottom: 26, left: 44 };
/** Room after the last date for direct end labels. */
const END_LABEL_ROOM = 72;

export function shortDate(x: number): string {
  return new Date(x).toLocaleDateString(undefined, { month: "short", day: "numeric", timeZone: "UTC" });
}

export function LineChart({
  series,
  ariaLabel,
  formatY,
  formatX = shortDate,
  height = 220,
  onPointClick,
  zeroBaseline = false,
}: LineChartProps) {
  const [containerRef, width] = useWidth<HTMLDivElement>();
  const [active, setActive] = useState<number | null>(null);
  const tableId = useId();

  const xs = useMemo(
    () => [...new Set(series.flatMap((s) => s.points.filter((p) => p.y != null).map((p) => p.x)))].sort((a, b) => a - b),
    [series],
  );
  const values = series.flatMap((s) => s.points.flatMap((p) => (p.y == null ? [] : [p.y])));
  if (!xs.length || !values.length) return null;

  const ticks = niceTicks(zeroBaseline ? Math.min(0, ...values) : Math.min(...values), Math.max(...values));
  const yMin = ticks[0];
  const yMax = ticks[ticks.length - 1];
  const labelled = series.length > 1 && series.length <= 4;
  const plotW = Math.max(40, width - PAD.left - PAD.right - (labelled ? END_LABEL_ROOM : 0));
  const plotH = height - PAD.top - PAD.bottom;
  const xMin = xs[0];
  const xMax = xs[xs.length - 1];
  const xFor = (x: number) => PAD.left + (xMax === xMin ? plotW / 2 : ((x - xMin) / (xMax - xMin)) * plotW);
  const yFor = (y: number) => PAD.top + plotH - ((y - yMin) / (yMax - yMin || 1)) * plotH;

  // Date labels keep ~64px apart; the latest date always shows, displacing
  // whichever earlier label would crowd it.
  const shownLabels = new Set<number>();
  let lastShown = -Infinity;
  xs.forEach((x, i) => {
    if (xFor(x) - lastShown >= 64) {
      shownLabels.add(i);
      lastShown = xFor(x);
    }
  });
  if (!shownLabels.has(xs.length - 1)) {
    const kept = [...shownLabels];
    const previous = kept[kept.length - 1];
    if (previous !== undefined && previous !== 0 && xFor(xs[xs.length - 1]) - xFor(xs[previous]) < 64) shownLabels.delete(previous);
    if (xs.length > 1 && xFor(xs[xs.length - 1]) - xFor(xs[0]) >= 64) shownLabels.add(xs.length - 1);
  }

  const nearest = (clientX: number, rect: DOMRect) => {
    const px = ((clientX - rect.left) / rect.width) * width;
    let best = 0;
    xs.forEach((x, i) => {
      if (Math.abs(xFor(x) - px) < Math.abs(xFor(xs[best]) - px)) best = i;
    });
    return best;
  };
  const onMove = (event: PointerEvent<SVGSVGElement>) => setActive(nearest(event.clientX, event.currentTarget.getBoundingClientRect()));
  const onKey = (event: KeyboardEvent<SVGSVGElement>) => {
    if (event.key === "ArrowRight") setActive((i) => Math.min(xs.length - 1, (i ?? -1) + 1));
    else if (event.key === "ArrowLeft") setActive((i) => Math.max(0, (i ?? xs.length) - 1));
    else if (event.key === "Escape") setActive(null);
    else if (event.key === "Enter" && active != null && onPointClick) onPointClick(series[0].key, xs[active]);
    else return;
    event.preventDefault();
  };

  // End labels: only for ≤4 series, and only when they sit ≥14px apart.
  const ends = series
    .map((s) => {
      const last = [...s.points].reverse().find((p) => p.y != null);
      return last ? { s, y: yFor(last.y as number), value: last.y as number, x: xFor(last.x) } : null;
    })
    .filter((e): e is NonNullable<typeof e> => e != null);
  const sortedEnds = [...ends].sort((a, b) => a.y - b.y);
  const endLabels = labelled && sortedEnds.every((e, i) => i === 0 || e.y - sortedEnds[i - 1].y >= 14);

  const activeX = active != null ? xs[active] : null;
  const tooltipLeft = activeX != null ? Math.min(Math.max(xFor(activeX) + 10, 0), width - 170) : 0;

  return (
    <div ref={containerRef} className="relative w-full">
      <svg
        role="img"
        aria-label={ariaLabel}
        aria-describedby={tableId}
        tabIndex={0}
        width={width}
        height={height}
        className="block outline-none focus-visible:ring-2 focus-visible:ring-ring"
        onPointerMove={onMove}
        onPointerLeave={() => setActive(null)}
        onKeyDown={onKey}
        onClick={() => {
          if (active != null && onPointClick) onPointClick(series[0].key, xs[active]);
        }}
        style={{ cursor: onPointClick ? "pointer" : "default" }}
      >
        {ticks.map((tick) => (
          <g key={tick}>
            <line x1={PAD.left} x2={PAD.left + plotW} y1={yFor(tick)} y2={yFor(tick)} stroke="var(--border-soft)" strokeWidth={1} />
            <text x={PAD.left - 8} y={yFor(tick)} dy="0.32em" textAnchor="end" fontSize={11} fill="var(--muted-foreground)" style={{ fontVariantNumeric: "tabular-nums" }}>
              {formatY(tick)}
            </text>
          </g>
        ))}
        {xs.map((x, i) =>
          shownLabels.has(i) ? (
            <text key={x} x={xFor(x)} y={height - 8} textAnchor={xs.length === 1 ? "middle" : i === 0 ? "start" : i === xs.length - 1 ? "end" : "middle"} fontSize={11} fill="var(--muted-foreground)">
              {formatX(x)}
            </text>
          ) : null,
        )}
        {activeX != null && (
          <line x1={xFor(activeX)} x2={xFor(activeX)} y1={PAD.top} y2={PAD.top + plotH} stroke="var(--muted-foreground)" strokeWidth={1} opacity={0.6} />
        )}
        {series.map((s) => {
          const segments: string[][] = [[]];
          s.points
            .slice()
            .sort((a, b) => a.x - b.x)
            .forEach((p) => {
              if (p.y == null) segments.push([]);
              else segments[segments.length - 1].push(`${xFor(p.x)},${yFor(p.y)}`);
            });
          return (
            <g key={s.key}>
              {segments.filter((seg) => seg.length > 1).map((seg, i) => (
                <polyline key={i} points={seg.join(" ")} fill="none" stroke={s.color} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
              ))}
              {s.points.map((p) =>
                p.y == null ? null : (
                  <circle
                    key={p.x}
                    cx={xFor(p.x)}
                    cy={yFor(p.y)}
                    r={activeX === p.x ? 5 : 4}
                    fill={s.color}
                    stroke="var(--card)"
                    strokeWidth={2}
                  />
                ),
              )}
            </g>
          );
        })}
      </svg>

      {activeX != null && (
        <div
          role="status"
          className="pointer-events-none absolute top-1 z-10 min-w-36 rounded-md border border-border bg-popover px-2.5 py-2 text-xs shadow-lg"
          style={{ left: tooltipLeft }}
        >
          <p className="mb-1 text-[0.65rem] uppercase tracking-wider text-muted-foreground">{formatX(activeX)}</p>
          {series.map((s) => {
            const point = s.points.find((p) => p.x === activeX);
            return (
              <p key={s.key} className="flex items-center gap-2">
                <span aria-hidden className="inline-block h-0.5 w-3 rounded" style={{ background: s.color }} />
                <span className="font-semibold text-foreground">{point?.y != null ? formatY(point.y) : "—"}</span>
                <span className="text-muted-foreground">{s.label}</span>
              </p>
            );
          })}
        </div>
      )}

      {endLabels && (
        <div aria-hidden className="pointer-events-none absolute inset-0">
          {ends.map((e) => (
            <span key={e.s.key} className="absolute max-w-[68px] -translate-y-1/2 truncate text-[0.65rem] text-muted-foreground" style={{ left: e.x + 8, top: e.y }}>
              {e.s.label}
            </span>
          ))}
        </div>
      )}

      {series.length > 1 && (
        <ul className="mt-1 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground" aria-label="Legend">
          {series.map((s) => (
            <li key={s.key} className="flex items-center gap-1.5">
              <span aria-hidden className="inline-block h-0.5 w-4 rounded" style={{ background: s.color }} />
              {s.label}
            </li>
          ))}
        </ul>
      )}

      <details className="mt-1 text-xs">
        <summary className="cursor-pointer text-muted-foreground hover:text-foreground">Table view</summary>
        <table id={tableId} className="mt-2 w-full text-left" style={{ fontVariantNumeric: "tabular-nums" }}>
          <thead>
            <tr className="text-muted-foreground">
              <th className="py-1 pr-3 font-medium">Date</th>
              {series.map((s) => (
                <th key={s.key} className="py-1 pr-3 font-medium">{s.label}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {xs.map((x) => (
              <tr key={x} className="border-t border-border-soft">
                <td className="py-1 pr-3">{formatX(x)}</td>
                {series.map((s) => {
                  const point = s.points.find((p) => p.x === x);
                  return <td key={s.key} className="py-1 pr-3">{point?.y != null ? formatY(point.y) : "—"}</td>;
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </details>
    </div>
  );
}
