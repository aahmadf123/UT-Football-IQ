"use client";

/**
 * The one status→color mapping in the app. Every surface that renders a job
 * state, video lifecycle state, or alert severity goes through these helpers —
 * replacing the six divergent per-file maps the old UI accumulated.
 *
 * Gold is brand, never a status color.
 */

import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

export type StatusTone = "ok" | "warn" | "danger" | "info" | "neutral";

const TONE_CLASS: Record<StatusTone, string> = {
  ok: "border-status-ok/40 bg-status-ok/15 text-status-ok",
  warn: "border-status-warn/40 bg-status-warn/15 text-status-warn",
  danger: "border-status-danger/40 bg-status-danger/15 text-status-danger",
  info: "border-status-info/40 bg-status-info/15 text-status-info",
  neutral: "border-border-soft bg-secondary/50 text-muted-foreground",
};

const TONE_DOT: Record<StatusTone, string> = {
  ok: "bg-status-ok",
  warn: "bg-status-warn",
  danger: "bg-status-danger",
  info: "bg-status-info",
  neutral: "bg-muted-foreground",
};

export function StatusBadge({
  tone,
  children,
  dot = false,
  className,
  ...props
}: React.ComponentProps<"span"> & { tone: StatusTone; dot?: boolean }) {
  return (
    <Badge
      variant="outline"
      className={cn("gap-1.5 font-medium uppercase tracking-wide", TONE_CLASS[tone], className)}
      {...props}
    >
      {dot ? <span aria-hidden className={cn("size-1.5 rounded-full", TONE_DOT[tone])} /> : null}
      {children}
    </Badge>
  );
}

/** Processing-job status (backend `processing_jobs.status`). */
export function toneForJobStatus(status: string): StatusTone {
  switch (status.toLowerCase()) {
    case "succeeded":
      return "ok";
    case "running":
      return "info";
    case "failed":
      return "danger";
    default:
      return "neutral";
  }
}

/** Video upload→processing lifecycle status. */
export function toneForVideoStatus(status: string): StatusTone {
  switch (status.toLowerCase()) {
    case "ready":
      return "ok";
    case "processing":
      return "info";
    case "queued":
      return "warn";
    case "failed":
      return "danger";
    default:
      return "neutral";
  }
}

/** Coaching-alert severity. */
export function toneForSeverity(severity: string): StatusTone {
  switch (severity.toLowerCase()) {
    case "critical":
    case "high":
      return "danger";
    case "warning":
    case "medium":
      return "warn";
    case "low":
      return "ok";
    default:
      return "info";
  }
}
