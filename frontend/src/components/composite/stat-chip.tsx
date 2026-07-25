"use client";

import { cn } from "@/lib/utils";

/**
 * Scoreboard-style metric: condensed uppercase label over a mono tabular
 * numeral. The one way numbers are presented across the app. Absent metrics
 * render "—", never a fabricated value (repo rule #96).
 */
export function StatChip({
  label,
  value,
  hint,
  className,
  ...props
}: React.ComponentProps<"div"> & {
  label: string;
  value: React.ReactNode;
  hint?: string;
}) {
  return (
    <div
      className={cn(
        "flex min-w-0 flex-col gap-0.5 rounded-md border border-border-soft bg-secondary/40 px-3 py-2",
        className,
      )}
      {...props}
    >
      <span className="truncate font-display text-[0.7rem] font-semibold uppercase tracking-widest text-muted-foreground">
        {label}
      </span>
      <span data-numeric className="font-mono text-lg font-semibold leading-tight text-foreground">
        {value}
      </span>
      {hint ? <span className="truncate text-[0.68rem] text-muted-foreground">{hint}</span> : null}
    </div>
  );
}

/** Compact label/value line for dense lists (replaces the old MetricLine). */
export function StatLine({
  label,
  value,
  className,
  ...props
}: React.ComponentProps<"div"> & { label: string; value: React.ReactNode }) {
  return (
    <div className={cn("flex items-baseline justify-between gap-3 text-sm", className)} {...props}>
      <span className="text-muted-foreground">{label}</span>
      <span data-numeric className="font-mono text-[0.82rem] font-semibold text-foreground">
        {value}
      </span>
    </div>
  );
}
