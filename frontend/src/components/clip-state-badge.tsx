"use client";

/**
 * Same-session result-state badges (Issue #147).
 *
 * A coach reviewing a clip needs to know two things at a glance:
 *   1. Is this a *preliminary* first pass from the 90-second same-session loop,
 *      or the nightly *final* result? — the "Preliminary" pill.
 *   2. Where does it sit in the review queue? — reviewed / low-confidence /
 *      needs-review.
 *
 * Both are derived on the backend (``is_preliminary`` / ``review_state`` on the
 * clip payload) so the UI never recomputes confidence policy.
 */
import type { ClipReviewState } from "@/lib/types";
import { StatusBadge, type StatusTone } from "@/components/composite/status-badge";

export function PreliminaryBadge() {
  return (
    <StatusBadge
      tone="warn"
      aria-label="Preliminary result — same-session first pass, not yet upgraded by nightly processing"
      data-testid="preliminary-badge"
    >
      Preliminary
    </StatusBadge>
  );
}

const REVIEW_META: Record<ClipReviewState, { label: string; tone: StatusTone; aria: string }> = {
  reviewed: {
    label: "Reviewed",
    tone: "ok",
    aria: "Reviewed — a coach has signed off on this clip",
  },
  low_confidence: {
    label: "Low confidence",
    tone: "danger",
    aria: "Low confidence — review this clip before trusting its labels",
  },
  needs_review: {
    label: "Needs review",
    tone: "neutral",
    aria: "Needs review — a first-pass result nobody has confirmed yet",
  },
};

export function ReviewStateBadge({ state }: { state: ClipReviewState }) {
  const meta = REVIEW_META[state];
  return (
    <StatusBadge tone={meta.tone} aria-label={meta.aria} data-testid={`review-state-${state}`}>
      {meta.label}
    </StatusBadge>
  );
}

/** Convenience: render the preliminary + review pills for a clip together. */
export function ClipStateBadges({
  isPreliminary,
  reviewState,
}: {
  isPreliminary?: boolean;
  reviewState?: ClipReviewState;
}) {
  if (!isPreliminary && !reviewState) return null;
  return (
    <span className="inline-flex items-center gap-1.5">
      {isPreliminary && <PreliminaryBadge />}
      {reviewState && <ReviewStateBadge state={reviewState} />}
    </span>
  );
}
