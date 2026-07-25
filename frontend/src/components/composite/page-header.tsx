"use client";

import { pageTitles } from "@/lib/labels";
import type { PageKey } from "@/lib/types";
import { useAppState } from "@/lib/app-state";
import { MockBadge } from "@/components/mock-badge";

/**
 * Standard page opener: display-face title, subtitle, data-source badge, and
 * an actions slot — closed with the yard-hash rule (the app's one decorative
 * structural device).
 */
export function PageHeader({
  page,
  title,
  subtitle,
  actions,
}: {
  page?: PageKey;
  title?: string;
  subtitle?: string;
  actions?: React.ReactNode;
}) {
  const { apiStatus } = useAppState();
  const entry = page ? pageTitles[page] : undefined;
  const resolvedTitle = title ?? entry?.title ?? "";
  const resolvedSubtitle = subtitle ?? entry?.subtitle;

  return (
    <header className="mb-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h1 className="flex flex-wrap items-center gap-2 font-display text-2xl font-semibold uppercase leading-tight tracking-wide text-foreground">
            {resolvedTitle}
            <MockBadge status={apiStatus} />
          </h1>
          {resolvedSubtitle ? (
            <p className="mt-0.5 max-w-3xl text-[0.82rem] text-muted-foreground">{resolvedSubtitle}</p>
          ) : null}
        </div>
        {actions ? <div className="flex shrink-0 items-center gap-2">{actions}</div> : null}
      </div>
      <div aria-hidden className="hash-rule mt-3" />
    </header>
  );
}
