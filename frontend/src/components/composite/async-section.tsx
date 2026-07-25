"use client";

import { CloudOff, Inbox } from "lucide-react";
import type { FetchState } from "@/lib/fetch-state";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState } from "@/components/composite/empty-state";

/**
 * The one renderer for the canonical FetchState lifecycle. Every data-backed
 * section renders through this so loading/offline/error/empty look and speak
 * the same everywhere — and so offline copy never leaks env-var names to a
 * coach (repo rule #96: honest states, no fabricated data).
 */

export const OFFLINE_COPY = {
  title: "Not connected to the team server",
  hint: "Data will appear when the system is back online.",
} as const;

export function AsyncSection<T>({
  state,
  onRetry,
  children,
  loadingLabel = "Loading",
  loadingRows = 3,
  offlineTitle = OFFLINE_COPY.title,
  offlineHint = OFFLINE_COPY.hint,
  emptyTitle = "Nothing here yet",
  emptyHint,
  errorTitle = "Couldn't load this section",
}: {
  state: FetchState<T>;
  onRetry?: () => void;
  children: (data: T) => React.ReactNode;
  loadingLabel?: string;
  loadingRows?: number;
  offlineTitle?: string;
  offlineHint?: string;
  emptyTitle?: string;
  emptyHint?: string;
  errorTitle?: string;
}) {
  switch (state.kind) {
    case "loading":
      return (
        <div
          role="status"
          aria-busy="true"
          aria-live="polite"
          aria-label={loadingLabel}
          className="flex flex-col gap-2 py-1"
        >
          {Array.from({ length: loadingRows }, (_, i) => (
            <Skeleton key={i} className="h-8 w-full" />
          ))}
          <span className="sr-only">{loadingLabel}…</span>
        </div>
      );
    case "offline":
      return <EmptyState icon={CloudOff} title={offlineTitle} hint={offlineHint} data-testid="section-offline" />;
    case "error":
      return (
        <Alert variant="destructive" role="alert" data-testid="section-error">
          <AlertTitle>{errorTitle}</AlertTitle>
          <AlertDescription className="break-words">{state.message}</AlertDescription>
          {onRetry ? (
            <Button variant="outline" size="sm" className="mt-2 w-fit" onClick={onRetry}>
              Retry
            </Button>
          ) : null}
        </Alert>
      );
    case "empty":
      return <EmptyState icon={Inbox} title={emptyTitle} hint={emptyHint} data-testid="section-empty" />;
    case "ready":
      return <>{children(state.data)}</>;
  }
}
