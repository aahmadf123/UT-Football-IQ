/**
 * Jersey-number identity card for a player (no photo pipeline yet — renders
 * the real jersey/position/name from the roster).
 */

import type { PlayerSummary } from "@/lib/types";
import { cn } from "@/lib/utils";

export function PlayerPortrait({
  player,
  compact = false,
}: {
  player: PlayerSummary;
  compact?: boolean;
}) {
  return (
    <div
      className={cn(
        "grid content-end rounded-lg border border-border-soft bg-gradient-to-br from-[oklch(0.2_0.05_252)] to-[oklch(0.33_0.09_252)]",
        compact ? "min-h-21 p-2.5" : "min-h-31 p-3.5",
      )}
    >
      <strong
        data-numeric
        className={cn("font-mono leading-none", compact ? "text-2xl" : "text-4xl")}
      >
        #{player.jersey}
      </strong>
      <span
        className={cn(
          "mt-1 font-display font-semibold uppercase tracking-wide text-muted-foreground",
          compact ? "text-[0.78rem]" : "text-sm",
        )}
      >
        {player.position} · {player.name}
      </span>
    </div>
  );
}
