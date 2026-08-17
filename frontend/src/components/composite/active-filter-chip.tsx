"use client";

import { X } from "lucide-react";
import { SESSION_LABELS, useAppState } from "@/lib/app-state";
import { formatDateLabel } from "@/components/composite/filter-bar";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

/**
 * Dismissible chip surfacing the global film filter (session type + date from
 * AppState, set via FilterBar on Film Room / Model Insights). Render it on
 * pages whose data is silently narrowed by that state but which do not render
 * the FilterBar itself, so a coach can see — and clear — why film is missing.
 * Renders nothing while the filter is at its default (all sessions, all dates).
 */
export function ActiveFilterChip({ className }: { className?: string }) {
  const { sessionType, setSessionType, selectedDate, setSelectedDate } = useAppState();

  const filtered = sessionType !== "all" || selectedDate !== "";
  if (!filtered) return null;

  const parts: string[] = [];
  if (sessionType !== "all") parts.push(SESSION_LABELS[sessionType]);
  if (selectedDate) parts.push(formatDateLabel(selectedDate));

  const clear = () => {
    setSessionType("all");
    setSelectedDate("");
  };

  return (
    <div
      role="status"
      data-testid="active-filter-chip"
      className={cn(
        "mb-4 flex w-fit max-w-full items-center gap-2 rounded-full border border-border-soft bg-secondary/40 py-1 pl-3 pr-1",
        className,
      )}
    >
      <span className="truncate text-xs text-muted-foreground">
        Filtered: <span className="font-semibold text-foreground">{parts.join(" · ")}</span>
      </span>
      <Button
        type="button"
        variant="ghost"
        size="sm"
        className="h-6 rounded-full px-2 text-xs"
        onClick={clear}
        aria-label="Clear film filter"
        data-testid="active-filter-clear"
      >
        <X className="size-3" /> Clear
      </Button>
    </div>
  );
}
