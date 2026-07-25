"use client";

import { Badge } from "@/components/ui/badge";

/**
 * Badge that marks a surface as EXPERIMENTAL (Issue #10). Frontier analytics
 * (xSep/xYards/xPressure) and in-game pattern breaks are derived from tracking
 * outputs that are not yet validated for Toledo — this badge makes sure a coach
 * never mistakes them for a trusted result.
 */
export function ExperimentalBadge({ label = "Experimental" }: { label?: string }) {
  return (
    <Badge
      variant="outline"
      className="border-(--violet)/50 bg-(--violet)/10 text-[0.65rem] font-semibold uppercase tracking-wide text-(--violet)"
      aria-label={`${label} metric — not a validated result`}
      data-testid="experimental-badge"
    >
      {label}
    </Badge>
  );
}
