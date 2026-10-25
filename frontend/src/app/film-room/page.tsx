"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense } from "react";
import { FootballShell } from "@/components/shell/app-shell";
import { SessionWorkspace } from "@/components/film-room/session-workspace";
import { ReviewTab } from "@/components/film-room/review-tab";
import { UploadProcessFilm } from "@/components/film-room/upload-process";
import { useUploadWidget } from "@/components/shared/upload-widget";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";
import { FolderOpen, ListVideo, Activity } from "lucide-react";

// "Clips & Highlights" was folded away with the mock clip grid (#96): Browse
// Film is the clip library (real sessions → videos → clips), Review & Tag
// Plays is the per-video clip inventory that deep-links into clip review.
const TABS = [
  { key: "browse", label: "Sessions & film", icon: FolderOpen },
  { key: "review", label: "Review plays", icon: ListVideo },
  { key: "upload", label: "Upload & processing", icon: Activity },
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
  const selectedSessionId = searchParams.get("sessionId") ?? "";
  const selectSession = (id: string) => {
    const url = new URL(window.location.href);
    if (id !== (url.searchParams.get("sessionId") ?? "")) url.searchParams.delete("videoId");
    if (id) url.searchParams.set("sessionId", id); else url.searchParams.delete("sessionId");
    window.history.replaceState(null, "", url);
  };

  const { openFilePicker: handleUploadClick, widget } = useUploadWidget({
    successMessage: (count) =>
      `Uploaded ${count} recording${count === 1 ? "" : "s"} — track progress in Upload & processing.`,
  });

  return (
    <>
      {widget}

      <nav
        aria-label="Film Room sections"
        className="mb-5 flex max-w-full gap-5 overflow-x-auto border-b border-border-soft"
      >
        {TABS.map((tab) => (
          <Link
            key={tab.key}
            href={`/film-room/?tab=${tab.key}${selectedSessionId ? `&sessionId=${encodeURIComponent(selectedSessionId)}` : ""}`}
            aria-current={tab.key === activeTab ? "page" : undefined}
            data-testid={`film-room-tab-${tab.key}`}
            className={cn(
              "inline-flex min-h-12 items-center gap-2 whitespace-nowrap border-b-2 px-1 pb-3 text-[13px] font-medium transition-colors",
              tab.key === activeTab
                ? "border-primary text-primary"
                : "border-transparent text-muted-foreground hover:text-foreground",
            )}
          >
            <tab.icon className="size-4" aria-hidden />{tab.label}
          </Link>
        ))}
      </nav>

      {activeTab === "browse" && <SessionWorkspace initialSessionId={selectedSessionId} onSelect={selectSession} />}
      {activeTab === "review" && <ReviewTab initialVideoId={videoIdParam ?? undefined} sessionId={selectedSessionId || undefined} onSessionChange={selectSession} />}
      {activeTab === "upload" && <UploadProcessFilm onUploadClick={handleUploadClick} />}
    </>
  );
}
