"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense } from "react";
import { FootballShell } from "@/components/shell/app-shell";
import { LibraryView } from "@/app/library/library-view";
import { ReviewTab } from "@/components/film-room/review-tab";
import { UploadProcessFilm } from "@/components/film-room/upload-process";
import { useUploadWidget } from "@/components/shared/upload-widget";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";

// "Clips & Highlights" was folded away with the mock clip grid (#96): Browse
// Film is the clip library (real sessions → videos → clips), Review & Tag
// Plays is the per-video clip inventory that deep-links into clip review.
const TABS = [
  { key: "browse", label: "Browse Film" },
  { key: "review", label: "Review & Tag Plays" },
  { key: "upload", label: "Upload / Process Film" },
] as const;

type TabKey = (typeof TABS)[number]["key"];

function isTabKey(value: string | null): value is TabKey {
  return TABS.some((t) => t.key === value);
}

export default function FilmRoomPage() {
  return (
    <FootballShell activePage="film-room">
      <Suspense
        fallback={
          <div role="status" aria-label="Loading Film Room" className="flex flex-col gap-2">
            <Skeleton className="h-9 w-80" />
            <Skeleton className="h-40 w-full" />
          </div>
        }
      >
        <FilmRoomContent />
      </Suspense>
    </FootballShell>
  );
}

function FilmRoomContent() {
  const searchParams = useSearchParams();
  const tabParam = searchParams.get("tab");
  const activeTab: TabKey = isTabKey(tabParam) ? tabParam : "browse";
  const videoIdParam = searchParams.get("videoId");

  const { openFilePicker: handleUploadClick, widget } = useUploadWidget({
    successMessage: (count) =>
      `Uploaded ${count} clip${count === 1 ? "" : "s"} — track processing in the Upload / Process Film tab.`,
  });

  return (
    <>
      {widget}

      <nav
        aria-label="Film Room sections"
        className="mb-4 flex w-fit max-w-full gap-1 overflow-x-auto rounded-lg border border-border-soft bg-secondary/40 p-1"
      >
        {TABS.map((tab) => (
          <Link
            key={tab.key}
            href={`/film-room/?tab=${tab.key}`}
            aria-current={tab.key === activeTab ? "page" : undefined}
            data-testid={`film-room-tab-${tab.key}`}
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

      {activeTab === "browse" && <LibraryView />}
      {activeTab === "review" && <ReviewTab initialVideoId={videoIdParam ?? undefined} />}
      {activeTab === "upload" && <UploadProcessFilm onUploadClick={handleUploadClick} />}
    </>
  );
}
