"use client";

import type { ApiStatus } from "@/lib/app-state";
import { Badge } from "@/components/ui/badge";

/**
 * Data-source honesty badge (repo rule #96): gold = mock data, warn-outline =
 * API offline. Hidden while live/loading so real data carries no noise.
 */
export function MockBadge({ status }: { status: ApiStatus }) {
  if (status === "live" || status === "idle" || status === "loading") return null;

  const mock = status === "mock";
  const label = mock ? "Mock data" : "API offline";

  return (
    <Badge
      variant={mock ? "default" : "outline"}
      className={
        mock
          ? "text-[0.65rem] font-semibold uppercase tracking-wide"
          : "border-status-warn/50 bg-status-warn/15 text-[0.65rem] font-semibold uppercase tracking-wide text-status-warn"
      }
      aria-label={`Data source: ${label}`}
      data-testid="mock-badge"
    >
      {label}
    </Badge>
  );
}
