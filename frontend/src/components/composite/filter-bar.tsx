"use client";

import { SESSION_LABELS, useAppState, type SessionType } from "@/lib/app-state";
import { Label } from "@/components/ui/label";
import { NativeSelect } from "@/components/ui/native-select";
import { cn } from "@/lib/utils";

export function formatDateLabel(value: string): string {
  if (!value) return "All dates";
  try {
    const d = new Date(value + "T12:00:00");
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
  } catch {
    return value;
  }
}

/**
 * Film filters (date + session type), rendered by the pages that actually
 * consume them. State stays in AppState so selection persists across pages —
 * only the control's home moved out of the global topbar.
 */
export function FilterBar({ className }: { className?: string }) {
  const { selectedDate, setSelectedDate, availableDates, sessionType, setSessionType } =
    useAppState();

  return (
    <div
      role="group"
      aria-label="Film filters"
      className={cn("mb-4 flex flex-wrap items-end gap-3", className)}
    >
      <div className="flex min-w-36 flex-col gap-1">
        <Label
          htmlFor="filter-date"
          className="font-display text-[0.68rem] font-semibold uppercase tracking-widest text-muted-foreground"
        >
          Practice date
        </Label>
        <NativeSelect
          id="filter-date"
          data-testid="filter-date"
          value={selectedDate}
          onChange={(e) => setSelectedDate(e.target.value)}
        >
          {availableDates.map((d) => (
            <option key={d} value={d}>
              {formatDateLabel(d)}
            </option>
          ))}
        </NativeSelect>
      </div>

      <div className="flex min-w-36 flex-col gap-1">
        <Label
          htmlFor="filter-session"
          className="font-display text-[0.68rem] font-semibold uppercase tracking-widest text-muted-foreground"
        >
          Session type
        </Label>
        <NativeSelect
          id="filter-session"
          data-testid="filter-session"
          value={sessionType}
          onChange={(e) => setSessionType(e.target.value as SessionType)}
        >
          {(Object.keys(SESSION_LABELS) as SessionType[]).map((k) => (
            <option key={k} value={k}>
              {SESSION_LABELS[k]}
            </option>
          ))}
        </NativeSelect>
      </div>
    </div>
  );
}
