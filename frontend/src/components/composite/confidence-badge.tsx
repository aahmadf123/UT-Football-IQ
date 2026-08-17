"use client";

/**
 * Identity-confidence badge — the one place the roster's identity vocabulary
 * is rendered. Buckets come from the backend and are never derived
 * client-side: `known` means human-confirmed, `probable` means the model is
 * confident, `needs_review` means the number should not be trusted yet (and
 * so no percentage is shown for it — a low-confidence percentage invites
 * exactly the trust it hasn't earned).
 */

import { StatusBadge, type StatusTone } from "@/components/composite/status-badge";
import type { IdentityBucket } from "@/lib/types";

const BUCKET_TONE: Record<IdentityBucket, StatusTone> = {
  known: "ok",
  probable: "info",
  needs_review: "warn",
};

const BUCKET_LABEL: Record<IdentityBucket, string> = {
  known: "Known",
  probable: "Probable",
  needs_review: "Needs review",
};

export function ConfidenceBadge({
  bucket,
  confidence,
  className,
}: {
  bucket: IdentityBucket | undefined;
  /** 0–1 confidence; only rendered for confident buckets. */
  confidence?: number;
  className?: string;
}) {
  if (!bucket) {
    return (
      <span className="text-muted-foreground" aria-label="No tracked film yet">
        —
      </span>
    );
  }
  const showPct = bucket !== "needs_review" && confidence != null;
  return (
    <StatusBadge tone={BUCKET_TONE[bucket]} dot className={className}>
      {BUCKET_LABEL[bucket]}
      {showPct ? (
        <span data-numeric className="font-mono">
          {Math.round(confidence * 100)}%
        </span>
      ) : null}
    </StatusBadge>
  );
}
