"use client";

import type { LucideIcon } from "lucide-react";
import { cn } from "@/lib/utils";

/**
 * Honest empty/idle surface (repo rule #96 — an unwired or empty view says so
 * instead of showing sample numbers). Copy is always coach-facing: no env-var
 * names, no stack traces.
 */
export function EmptyState({
  icon: Icon,
  title,
  hint,
  action,
  className,
  ...props
}: React.ComponentProps<"div"> & {
  icon?: LucideIcon;
  title: string;
  hint?: string;
  action?: React.ReactNode;
}) {
  return (
    <div
      className={cn(
        "flex flex-col items-center justify-center gap-1.5 rounded-lg border border-dashed border-border-soft px-6 py-8 text-center",
        className,
      )}
      {...props}
    >
      {Icon ? <Icon aria-hidden className="mb-1 size-5 text-muted-foreground/70" /> : null}
      <p className="text-sm font-medium text-foreground">{title}</p>
      {hint ? <p className="max-w-prose text-xs text-muted-foreground">{hint}</p> : null}
      {action ? <div className="mt-2">{action}</div> : null}
    </div>
  );
}
