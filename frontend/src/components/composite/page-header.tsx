"use client";

import { pageTitles } from "@/lib/labels";
import type { PageKey } from "@/lib/types";
import { useAppState } from "@/lib/app-state";
import { MockBadge } from "@/components/mock-badge";
import { useWorkspaceContext } from "@/lib/workspace-context";

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
  const workspace = useWorkspaceContext();
  const entry = page ? pageTitles[page] : undefined;
  const productTitles: Partial<Record<PageKey, { title: string; subtitle: string }>> = {
    dashboard: { title: "Today", subtitle: "Your film, processing progress, and next plays to review." },
    "film-room": { title: "Film Room", subtitle: "Organize sessions, work with every camera angle, and review plays." },
    "clip-review": { title: "Clip Review", subtitle: "Watch the play, inspect player tracking, and verify your coaching labels." },
    settings: { title: workspace.settingsLabel, subtitle: "Processing connections, available features, and library settings." },
    scouting: { title: "Scouting", subtitle: "Explore Toledo tendencies and prepare for your next opponent." },
    players: { title: "Players", subtitle: "Your roster and the film evidence behind player development." },
    reports: { title: "Reports", subtitle: "Build and download a coaching packet from the film you choose." },
  };
  const productEntry = page ? productTitles[page] : undefined;
  const resolvedTitle = title ?? productEntry?.title ?? entry?.title ?? "";
  const resolvedSubtitle = subtitle ?? productEntry?.subtitle ?? entry?.subtitle;

  return (
    <header className="mb-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="section-eyebrow mb-2">Toledo Football <span className="mx-1.5 text-primary/50">/</span> {workspace.libraryLabel}</p>
          <h1 className="flex flex-wrap items-center gap-2 text-[1.875rem] font-semibold leading-tight tracking-tight text-foreground">
            {resolvedTitle}
            <MockBadge status={apiStatus} />
          </h1>
          {resolvedSubtitle ? (
            <p className="mt-2 max-w-3xl text-sm leading-relaxed text-muted-foreground">{resolvedSubtitle}</p>
          ) : null}
        </div>
        {actions ? <div className="flex shrink-0 items-center gap-2">{actions}</div> : null}
      </div>
    </header>
  );
}
