/**
 * Run/pass tendency table shared by the analytics and scouting surfaces.
 * Renders grouping key, play count, and a run-rate bar per entry.
 */

import type { TendencyEntry } from "@/lib/types";

export function TendencyTable({ entries }: { entries: TendencyEntry[] }) {
  if (entries.length === 0) {
    return (
      <p className="text-xs text-muted-foreground">
        No tendencies above the minimum-sample threshold.
      </p>
    );
  }
  return (
    <div className="flex flex-col gap-1.5">
      {entries.map((e) => {
        const runPct = Math.round(e.run_rate * 100);
        return (
          <div
            key={e.grouping_key}
            className="grid grid-cols-[1fr_44px_minmax(90px,1fr)_60px] items-center gap-2 text-[0.82rem]"
          >
            <span className="truncate font-medium">{e.grouping_key}</span>
            <span data-numeric className="font-mono text-xs text-muted-foreground">
              {e.total_plays}
            </span>
            <div
              className="h-1.5 overflow-hidden rounded-full bg-secondary"
              role="img"
              aria-label={`${runPct}% run`}
            >
              <div className="h-full rounded-full bg-status-info" style={{ width: `${runPct}%` }} />
            </div>
            <span data-numeric className="text-right font-mono text-xs text-muted-foreground">
              {runPct}% run
            </span>
          </div>
        );
      })}
    </div>
  );
}
