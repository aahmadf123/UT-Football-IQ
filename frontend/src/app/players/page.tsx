"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense } from "react";
import { FootballShell } from "@/components/shell/app-shell";
import { PlayersView } from "@/components/players-view";
import { PlayerDevelopmentView } from "@/components/player-development-view";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";

// Player Development folded into Players (nav consolidation): Roster is the
// live identity/metrics table, Development is the per-player passport view.
const TABS = [
  { key: "roster", label: "Roster" },
  { key: "development", label: "Development" },
] as const;

type TabKey = (typeof TABS)[number]["key"];

function isTabKey(value: string | null): value is TabKey {
  return TABS.some((t) => t.key === value);
}

export default function PlayersPage() {
  return (
    <FootballShell activePage="players">
      <Suspense
        fallback={
          <div role="status" aria-label="Loading Players" className="flex flex-col gap-2">
            <Skeleton className="h-9 w-60" />
            <Skeleton className="h-40 w-full" />
          </div>
        }
      >
        <PlayersContent />
      </Suspense>
    </FootballShell>
  );
}

function PlayersContent() {
  const searchParams = useSearchParams();
  const tabParam = searchParams.get("tab");
  const activeTab: TabKey = isTabKey(tabParam) ? tabParam : "roster";

  return (
    <>
      <nav
        aria-label="Players sections"
        className="mb-4 flex w-fit max-w-full gap-1 overflow-x-auto rounded-lg border border-border-soft bg-secondary/40 p-1"
      >
        {TABS.map((tab) => (
          <Link
            key={tab.key}
            href={`/players/?tab=${tab.key}`}
            aria-current={tab.key === activeTab ? "page" : undefined}
            data-testid={`players-tab-${tab.key}`}
            className={cn(
              "inline-flex min-h-9 items-center whitespace-nowrap rounded-md px-3 py-1.5 font-display text-[0.82rem] font-semibold uppercase tracking-wide transition-colors",
              tab.key === activeTab
                ? "bg-gradient-to-b from-primary to-gold-strong text-primary-foreground"
                : "text-muted-foreground hover:bg-accent hover:text-foreground",
            )}
          >
            {tab.label}
          </Link>
        ))}
      </nav>

      {activeTab === "roster" && <PlayersView />}
      {activeTab === "development" && <PlayerDevelopmentView />}
    </>
  );
}
