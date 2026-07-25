"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense } from "react";
import { FootballShell } from "@/components/shell/app-shell";
import { SelfScoutView } from "@/app/self-scout/self-scout-view";
import { OpponentScoutView } from "@/app/opponent-scout/opponent-scout-view";
import { CollegeDataView } from "@/app/college-data/college-data-view";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";

const TABS = [
  { key: "tendencies", label: "Our Tendencies" },
  { key: "opponent", label: "Opponent Prep" },
  { key: "college", label: "College Data" },
] as const;

type TabKey = (typeof TABS)[number]["key"];

function isTabKey(value: string | null): value is TabKey {
  return TABS.some((t) => t.key === value);
}

export default function ScoutingPage() {
  return (
    <FootballShell activePage="scouting">
      <Suspense
        fallback={
          <div role="status" aria-label="Loading Scouting" className="flex flex-col gap-2">
            <Skeleton className="h-9 w-72" />
            <Skeleton className="h-40 w-full" />
          </div>
        }
      >
        <ScoutingContent />
      </Suspense>
    </FootballShell>
  );
}

function ScoutingContent() {
  const searchParams = useSearchParams();
  const tabParam = searchParams.get("tab");
  const activeTab: TabKey = isTabKey(tabParam) ? tabParam : "tendencies";

  return (
    <>
      <nav
        aria-label="Scouting sections"
        className="mb-4 flex w-fit max-w-full gap-1 overflow-x-auto rounded-lg border border-border-soft bg-secondary/40 p-1"
      >
        {TABS.map((tab) => (
          <Link
            key={tab.key}
            href={`/scouting/?tab=${tab.key}`}
            aria-current={tab.key === activeTab ? "page" : undefined}
            data-testid={`scouting-tab-${tab.key}`}
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

      {activeTab === "tendencies" && <SelfScoutView />}
      {activeTab === "opponent" && <OpponentScoutView />}
      {activeTab === "college" && <CollegeDataView />}
    </>
  );
}
