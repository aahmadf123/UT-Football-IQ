/**
 * Stat tile: label · value · signed change vs a named period · sparkline.
 *
 * The change carries an arrow and words as well as colour, and its colour
 * follows direction × whether up is good. The sparkline is the quiet
 * de-emphasis line with the latest point in the accent.
 */

import { ArrowDownRight, ArrowRight, ArrowUpRight } from "lucide-react";
import { MUTED_LINE, SERIES_COLORS } from "./palette";

export function Sparkline({ values, width = 96, height = 28, label }: { values: (number | null)[]; width?: number; height?: number; label: string }) {
  const points = values.map((v, i) => ({ v, i })).filter((p): p is { v: number; i: number } => p.v != null);
  if (points.length < 2) return null;
  const min = Math.min(...points.map((p) => p.v));
  const max = Math.max(...points.map((p) => p.v));
  const x = (i: number) => 4 + (i / Math.max(1, values.length - 1)) * (width - 8);
  const y = (v: number) => 4 + (max === min ? (height - 8) / 2 : (1 - (v - min) / (max - min)) * (height - 8));
  const last = points[points.length - 1];
  return (
    <svg width={width} height={height} role="img" aria-label={label} className="shrink-0">
      <polyline points={points.map((p) => `${x(p.i)},${y(p.v)}`).join(" ")} fill="none" stroke={MUTED_LINE} strokeWidth={1.5} strokeLinejoin="round" strokeLinecap="round" opacity={0.7} />
      <circle cx={x(last.i)} cy={y(last.v)} r={3.5} fill={SERIES_COLORS[0]} stroke="var(--card)" strokeWidth={2} />
    </svg>
  );
}

export interface StatTileProps {
  label: string;
  value: string;
  unit?: string;
  /** Signed change; null hides the row. */
  change?: number | null;
  formatChange?: (value: number) => string;
  /** Period the change compares against, e.g. "vs previous week of film". */
  changeLabel?: string;
  upIsGood?: boolean;
  trend?: (number | null)[];
  hint?: string;
}

export function StatTile({ label, value, unit, change, formatChange = (v) => v.toFixed(1), changeLabel, upIsGood = true, trend, hint }: StatTileProps) {
  const flat = change != null && Math.abs(change) < 1e-9;
  const good = change != null && !flat && (change > 0) === upIsGood;
  const Arrow = change == null || flat ? ArrowRight : change > 0 ? ArrowUpRight : ArrowDownRight;
  return (
    <div className="flex min-w-0 flex-col gap-1 rounded-lg border border-border-soft bg-card p-4">
      <p className="text-xs text-muted-foreground">{label}</p>
      <div className="flex items-end justify-between gap-2">
        <p className="text-2xl font-semibold leading-none text-foreground">
          {value}
          {unit && value !== "—" ? <span className="ml-1 text-sm font-normal text-muted-foreground">{unit}</span> : null}
        </p>
        {trend && <Sparkline values={trend} label={`${label} trend`} />}
      </div>
      {change != null ? (
        <p className="flex items-center gap-1 text-xs">
          <Arrow aria-hidden className={flat ? "size-3.5 text-muted-foreground" : good ? "size-3.5 text-status-ok" : "size-3.5 text-status-danger"} />
          <span className={flat ? "text-muted-foreground" : good ? "text-status-ok" : "text-status-danger"}>
            {flat ? "No change" : `${change > 0 ? "+" : "−"}${formatChange(Math.abs(change))}`}
          </span>
          {changeLabel && <span className="text-muted-foreground">{changeLabel}</span>}
        </p>
      ) : (
        <p className="text-xs text-muted-foreground">{hint ?? "Needs a second week of film to compare"}</p>
      )}
    </div>
  );
}
