"use client";

/**
 * Film Room → Review & Tag Plays.
 *
 * Real per-video clip inventory: pick a video, list its processed clips from
 * /api/v1/videos/{id}/clips with play number / time range / result & review
 * state badges, and open each one in the real clip-review screen
 * (/clip-review/?clipId=…) with actual playback + overlays + corrections.
 * Accepts an initial video id so dashboard inbox rows deep-link straight in.
 */

import Link from "next/link";
import { ArrowRight } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { ClipStateBadges } from "@/components/clip-state-badge";
import { useAppState } from "@/lib/app-state";
import { useFetchState } from "@/lib/fetch-state";
import { fetchClipsForVideo } from "@/lib/api";
import { POSSESSION_LABEL, SESSION_KIND_LABEL } from "@/lib/labels";
import type { ApiClip, ApiVideo } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { NativeSelect } from "@/components/ui/native-select";
import { Skeleton } from "@/components/ui/skeleton";
import { Alert, AlertDescription } from "@/components/ui/alert";

export function ReviewTab({ initialVideoId }: { initialVideoId?: string }) {
  const { data, authToken, apiStatus } = useAppState();
  const videos = data.videos;
  const [selectedVideoId, setSelectedVideoId] = useState<string>(initialVideoId ?? "");

  // Default to the most recently created processed video; fall back to the
  // newest video of any status so the picker is never silently empty. A
  // deep-linked video id is honored once the video list contains it.
  useEffect(() => {
    if (videos.length === 0) return;
    if (selectedVideoId && videos.some((v) => v.id === selectedVideoId)) return;
    const sorted = [...videos].sort(
      (a, b) => (b.created_at ?? "").localeCompare(a.created_at ?? ""),
    );
    const preferred = sorted.find((v) => v.status === "ready") ?? sorted[0];
    setSelectedVideoId(preferred?.id ?? "");
  }, [videos, selectedVideoId]);

  const selectedVideo = videos.find((v) => v.id === selectedVideoId);

  const fetcher = useCallback(() => {
    if (!selectedVideoId) return Promise.resolve<ApiClip[]>([]);
    return fetchClipsForVideo(selectedVideoId, authToken);
  }, [selectedVideoId, authToken]);
  const { state, reload } = useFetchState(fetcher);

  const clips = useMemo(() => {
    if (state.kind !== "ready") return [];
    return [...state.data].sort((a, b) => {
      const ap = a.play_number ?? Number.MAX_SAFE_INTEGER;
      const bp = b.play_number ?? Number.MAX_SAFE_INTEGER;
      if (ap !== bp) return ap - bp;
      return a.start_time - b.start_time;
    });
  }, [state]);

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHeader className="flex flex-row flex-wrap items-start justify-between gap-3">
          <div>
            <h2 className="font-display text-base font-semibold uppercase tracking-wide">
              Review &amp; Tag Plays
            </h2>
            <p className="mt-0.5 max-w-2xl text-xs text-muted-foreground">
              Pick a video to see its detected plays. Each row opens the real clip-review screen —
              playback, tracking overlays, and coach corrections.
            </p>
          </div>
          <div className="flex min-w-64 flex-col gap-1">
            <Label
              htmlFor="review-video-picker"
              className="font-display text-[0.68rem] font-semibold uppercase tracking-widest text-muted-foreground"
            >
              Video
            </Label>
            <NativeSelect
              id="review-video-picker"
              value={selectedVideoId}
              onChange={(e) => setSelectedVideoId(e.target.value)}
              aria-label="Video to review"
              data-testid="review-video-picker"
              disabled={videos.length === 0}
            >
              {videos.length === 0 && <option value="">No videos yet</option>}
              {videos.map((v) => (
                <option key={v.id} value={v.id}>
                  {videoOptionLabel(v)}
                </option>
              ))}
            </NativeSelect>
          </div>
        </CardHeader>
      </Card>

      <Card>
        <CardHeader>
          <h2 className="font-display text-base font-semibold uppercase tracking-wide">
            Plays{selectedVideo ? ` — ${selectedVideo.filename}` : ""}
          </h2>
        </CardHeader>
        <CardContent>
          {videos.length === 0 ? (
            <p className="text-xs text-muted-foreground" data-testid="review-empty">
              {apiStatus === "loading" || apiStatus === "idle"
                ? "Loading film…"
                : apiStatus === "offline"
                  ? "Backend offline — processed clips appear when the team server is connected."
                  : "No film yet. Upload video from the Upload / Process Film tab; clips appear here after processing."}
            </p>
          ) : (
            <>
              {state.kind === "loading" && (
                <div role="status" aria-busy="true" aria-label="Loading clips" className="flex flex-col gap-2">
                  <Skeleton className="h-12 w-full" />
                  <Skeleton className="h-12 w-full" />
                </div>
              )}
              {state.kind === "offline" && (
                <p className="text-xs text-muted-foreground">
                  Backend offline — processed clips appear when the team server is connected.
                </p>
              )}
              {state.kind === "error" && (
                <Alert variant="destructive" role="alert">
                  <AlertDescription>{state.message}</AlertDescription>
                  <Button variant="outline" size="sm" className="mt-2 w-fit" onClick={reload}>
                    Retry
                  </Button>
                </Alert>
              )}
              {state.kind === "empty" && (
                <p className="text-xs text-muted-foreground" data-testid="review-no-clips">
                  No clips processed for this video yet.
                  {selectedVideo?.status === "uploaded"
                    ? " Start processing from the Upload / Process Film tab."
                    : selectedVideo?.status === "processing"
                      ? " Processing is still running — clips appear as play detection finishes."
                      : ""}
                </p>
              )}
              {state.kind === "ready" && (
                <div className="flex flex-col gap-2">
                  {clips.map((clip) => (
                    <ReviewClipRow key={clip.id} clip={clip} />
                  ))}
                </div>
              )}
            </>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

function ReviewClipRow({ clip }: { clip: ApiClip }) {
  const possession = clip.our_possession ?? clip.side_of_ball ?? null;
  const possessionLabel = possession ? POSSESSION_LABEL[possession] : null;
  const isPreliminary =
    clip.is_preliminary === true || clip.result_state === "preliminary";
  return (
    <Link
      href={`/clip-review/?clipId=${encodeURIComponent(clip.id)}`}
      data-testid={`review-clip-${clip.id}`}
      className="group flex items-center justify-between gap-3 rounded-md border border-border-soft px-3 py-2.5 transition-colors hover:border-border hover:bg-accent/50"
    >
      <div className="min-w-0">
        <span className="text-[0.85rem] font-semibold">
          {clip.play_number != null ? `Play #${clip.play_number}` : `Clip ${clip.id.slice(0, 8)}`}
        </span>
        <div data-numeric className="mt-0.5 font-mono text-xs text-muted-foreground">
          {formatClock(clip.start_time)}–{formatClock(clip.end_time)}
          {" · "}
          {(clip.end_time - clip.start_time).toFixed(1)}s
          {possessionLabel ? ` · ${possessionLabel}` : ""}
          {clip.session_kind ? ` · ${SESSION_KIND_LABEL[clip.session_kind]}` : ""}
          {clip.confidence != null ? ` · ${Math.round(clip.confidence * 100)}%` : ""}
        </div>
      </div>
      <span className="flex shrink-0 items-center gap-2">
        <ClipStateBadges isPreliminary={isPreliminary} reviewState={clip.review_state} />
        <span className="inline-flex items-center gap-1 text-xs font-semibold uppercase tracking-wide text-primary opacity-80 transition-opacity group-hover:opacity-100">
          Review <ArrowRight className="size-3.5" />
        </span>
      </span>
    </Link>
  );
}

function videoOptionLabel(v: ApiVideo): string {
  const date = (v.recorded_at ?? v.created_at ?? "").slice(0, 10);
  return `${date ? `${date} · ` : ""}${v.filename} (${v.status})`;
}

function formatClock(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds));
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}
