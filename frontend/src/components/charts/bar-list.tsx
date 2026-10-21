/**
 * Horizontal bars for comparing a handful of named categories.
 *
 * Bars are ≤ 16px thick with a 4px rounded data-end, square at the
 * baseline; the value sits at the tip in ink, never in the bar's colour.
 * An optional threshold draws as a labelled hairline. Rows are plain
 * HTML, so the list doubles as its own table view.
 */

import type { ReactNode } from "react";

export interface BarRow {
  key: string;
  label: string;
  value: number | null;
  /** Extra marker after the value (e.g. a flag icon + word). */
  note?: ReactNode;
}

export function BarList({
  rows,
  max,
  color,
  format,
  threshold,
  ariaLabel,
}: {
  rows: BarRow[];
  max: number;
  color: string;
  format: (value: number) => string;
  threshold?: { value: number; label: string };
  ariaLabel: string;
}) {
  const pct = (value: number) => `${Math.max(0, Math.min(1, value / (max || 1))) * 100}%`;
  return (
    <div role="table" aria-label={ariaLabel} className="flex flex-col gap-2.5">
      {rows.map((row) => (
        <div key={row.key} role="row" className="grid grid-cols-[4.5rem_minmax(0,1fr)_10.5rem] items-center gap-3 text-xs">
          <span role="rowheader" className="truncate text-muted-foreground">{row.label}</span>
          <span role="cell" className="relative h-4">
            {row.value != null && (
              <span className="absolute inset-y-0 left-0 rounded-r" style={{ width: pct(row.value), background: color }} />
            )}
            {threshold && (
              <span aria-hidden className="absolute inset-y-[-3px] w-px bg-foreground/70" style={{ left: pct(threshold.value) }} />
            )}
          </span>
          <span role="cell" className="flex items-center gap-1.5 whitespace-nowrap font-semibold text-foreground" style={{ fontVariantNumeric: "tabular-nums" }}>
            {row.value != null ? format(row.value) : "—"}
            {row.note}
          </span>
        </div>
      ))}
      {threshold && (
        <p className="grid grid-cols-[4.5rem_minmax(0,1fr)_10.5rem] gap-3 text-[0.65rem] text-muted-foreground">
          <span />
          <span className="relative h-3">
            <span className="absolute -translate-x-1/2 whitespace-nowrap" style={{ left: pct(threshold.value) }}>{threshold.label}</span>
          </span>
          <span />
        </p>
      )}
    </div>
  );
}
