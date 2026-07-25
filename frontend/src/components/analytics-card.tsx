"use client";

/**
 * Shared analytics card state model (Issue #106).
 *
 * Every analytics surface in the app — dashboard tiles, the Analytics page,
 * the scout views — should render through this component so that
 * "live / loading / processing / gated / unavailable / error" are presented
 * consistently and so future metrics can plug in without re-implementing
 * gating logic.
 *
 * The model is intentionally explicit: we never want to render a hardcoded
 * number "as if" it were live. A card whose metric isn't wired yet should be
 * ``{ kind: "unavailable" }`` (or ``{ kind: "gated" }`` if it depends on a
 * plan/role), not a fabricated value.
 */

import type { ReactNode } from "react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { StatusBadge, type StatusTone } from "@/components/composite/status-badge";
import { cn } from "@/lib/utils";

export type AnalyticsCardState =
  /** Real backend data; render ``children``. */
  | { kind: "live"; updatedAt?: string | null }
  /** Request in flight or initial mount before the fetch resolves. */
  | { kind: "loading"; label?: string }
  /** The metric is queued/computing on the backend. */
  | { kind: "processing"; label?: string; etaSeconds?: number }
  /** The metric is implemented but not visible to this user/plan/role. */
  | { kind: "gated"; reason: string }
  /** The metric is not implemented yet, or has no data for these filters. */
  | { kind: "unavailable"; reason: string }
  /** Backend reachable but the response had no data. */
  | { kind: "empty"; reason: string }
  /** Request failed (network/5xx). */
  | { kind: "error"; message: string; onRetry?: () => void };

interface Props {
  title: string;
  state: AnalyticsCardState;
  /** Rendered only when ``state.kind === "live"``. */
  children?: ReactNode;
  /** Optional className on the outer card. */
  className?: string;
  /** Optional inline style on the outer card. */
  style?: React.CSSProperties;
  /** Optional element rendered to the right of the title (icon, link, etc). */
  headerExtra?: ReactNode;
  /**
   * Optional coach-readable sentence explaining *why* the metric is gated for
   * this specific footage (e.g. the calibration reason from the overlays
   * payload). Rendered only when ``state.kind === "gated"``, under the
   * generic gating reason.
   */
  gatedReason?: string;
}

export function AnalyticsCard({
  title,
  state,
  children,
  className,
  style,
  headerExtra,
  gatedReason,
}: Props) {
  return (
    <Card
      className={cn("gap-3", className)}
      style={style}
      data-testid={`analytics-card-${slug(title)}`}
      data-card-state={state.kind}
    >
      <CardHeader className="flex flex-row items-center justify-between gap-2">
        <h2 className="flex flex-wrap items-center gap-2 font-display text-sm font-semibold uppercase tracking-wide">
          {title}
          <StateBadge state={state} />
        </h2>
        {headerExtra}
      </CardHeader>
      <CardContent>
        {state.kind === "live" && children}
        {state.kind === "loading" && (
          <p className="text-xs text-muted-foreground" data-testid="card-loading">
            {state.label ?? "Loading…"}
          </p>
        )}
        {state.kind === "processing" && (
          <p className="text-xs text-muted-foreground" data-testid="card-processing">
            {state.label ?? "Processing… results will appear when the job completes."}
            {state.etaSeconds != null ? ` (~${state.etaSeconds}s)` : ""}
          </p>
        )}
        {state.kind === "empty" && (
          <p className="text-xs text-muted-foreground" data-testid="card-empty">
            {state.reason}
          </p>
        )}
        {state.kind === "unavailable" && (
          <p className="text-xs text-muted-foreground" data-testid="card-unavailable">
            {state.reason}
          </p>
        )}
        {state.kind === "gated" && (
          <div data-testid="card-gated">
            <p className="text-xs text-muted-foreground">{state.reason}</p>
            {gatedReason && (
              <p className="mt-1.5 text-xs text-foreground" data-testid="card-gated-reason">
                {gatedReason}
              </p>
            )}
          </div>
        )}
        {state.kind === "error" && (
          <div data-testid="card-error">
            <p className="text-xs text-status-danger">{state.message}</p>
            {state.onRetry && (
              <Button variant="outline" size="sm" className="mt-2" onClick={state.onRetry}>
                Retry
              </Button>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function StateBadge({ state }: { state: AnalyticsCardState }) {
  const meta = badgeMeta(state.kind);
  if (!meta) return null;
  return (
    <StatusBadge tone={meta.tone} data-testid={`card-badge-${state.kind}`}>
      {meta.label}
    </StatusBadge>
  );
}

function badgeMeta(
  kind: AnalyticsCardState["kind"],
): { label: string; tone: StatusTone } | null {
  switch (kind) {
    case "live":
      return null;
    case "loading":
      return { label: "Loading", tone: "info" };
    case "processing":
      return { label: "Processing", tone: "warn" };
    case "gated":
      return { label: "Locked", tone: "neutral" };
    case "unavailable":
      return { label: "Not yet", tone: "neutral" };
    case "empty":
      return { label: "Empty", tone: "neutral" };
    case "error":
      return { label: "Error", tone: "danger" };
  }
}

function slug(value: string): string {
  return value
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/(^-|-$)/g, "");
}
