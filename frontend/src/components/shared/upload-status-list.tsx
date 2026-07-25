"use client";

/**
 * Client-upload status list — the single rendering of in-browser upload
 * progress (previously duplicated between the page shell and the Film Room
 * upload tab). One `phaseLabel` / `phaseTone` mapping lives here.
 */

import { Trash2 } from "lucide-react";
import { useAppState, type UploadedClip, type UploadPhase } from "@/lib/app-state";
import { Button } from "@/components/ui/button";
import { Progress } from "@/components/ui/progress";
import { StatusBadge, type StatusTone } from "@/components/composite/status-badge";

export function phaseLabel(phase: UploadPhase): string {
  switch (phase) {
    case "idle":
      return "Queued to upload";
    case "requesting-url":
      return "Preparing…";
    case "uploading":
      return "Uploading…";
    case "registering":
      return "Saving…";
    case "done":
      return "Uploaded";
    case "error":
      return "Upload failed";
  }
}

export function phaseTone(phase: UploadPhase): StatusTone {
  switch (phase) {
    case "done":
      return "ok";
    case "error":
      return "danger";
    case "uploading":
    case "registering":
    case "requesting-url":
      return "info";
    default:
      return "neutral";
  }
}

export function UploadStatusList({ uploads }: { uploads: UploadedClip[] }) {
  const { retryUpload, removeUpload } = useAppState();
  if (uploads.length === 0) return null;
  return (
    <div className="flex flex-col gap-2">
      {uploads.map((u) => (
        <div
          key={u.id}
          className="flex flex-wrap items-center justify-between gap-3 rounded-md border border-border-soft p-2.5"
        >
          <div className="min-w-0 flex-1">
            <span className="text-[0.85rem] font-semibold">{u.filename}</span>
            <div className="mt-0.5 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
              <span data-numeric className="font-mono">
                {(u.sizeBytes / (1024 * 1024)).toFixed(1)} MB
              </span>
              <StatusBadge tone={phaseTone(u.phase)} dot>
                {phaseLabel(u.phase)}
                {u.phase === "uploading" && ` ${u.progress}%`}
              </StatusBadge>
            </div>
            {u.phase === "uploading" && (
              <Progress value={u.progress} className="mt-2 h-1.5" />
            )}
            {u.phase === "error" && u.error && (
              <div className="mt-1 text-xs text-status-danger">{u.error}</div>
            )}
          </div>
          <div className="flex shrink-0 items-center gap-2">
            {u.phase === "error" && (
              <Button variant="outline" size="sm" onClick={() => retryUpload(u.id)}>
                Retry upload
              </Button>
            )}
            <Button
              variant="ghost"
              size="icon"
              className="size-8"
              onClick={() => removeUpload(u.id)}
              aria-label={`Remove ${u.filename}`}
            >
              <Trash2 className="size-4" />
            </Button>
          </div>
        </div>
      ))}
    </div>
  );
}
