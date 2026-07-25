"use client";

import { ChevronDown } from "lucide-react";
import { cn } from "@/lib/utils";

/**
 * Styled native <select>. Used for data pickers (video, opponent, filters)
 * where native semantics beat a custom listbox: reliable in e2e, better on
 * touch devices, zero portal overhead.
 */
export function NativeSelect({
  className,
  children,
  ...props
}: React.ComponentProps<"select">) {
  return (
    <span className={cn("relative inline-flex min-w-0", className)}>
      <select
        className="h-9 w-full min-w-0 appearance-none truncate rounded-md border border-input bg-secondary/40 pl-3 pr-8 text-sm text-foreground transition-colors hover:border-border focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring disabled:cursor-not-allowed disabled:opacity-50"
        {...props}
      >
        {children}
      </select>
      <ChevronDown
        aria-hidden
        className="pointer-events-none absolute right-2.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground"
      />
    </span>
  );
}
