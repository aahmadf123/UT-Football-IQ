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
import { ArrowRight, RefreshCw } from "lucide-react";
import { useCallback, useMemo, useState } from "react";
import { ClipStateBadges } from "@/components/clip-state-badge";
import { useAppState } from "@/lib/app-state";
import { useFetchState } from "@/lib/fetch-state";
import { fetchAllVideos, fetchClipsForVideo } from "@/lib/api";
import { POSSESSION_LABEL, SESSION_KIND_LABEL } from "@/lib/labels";
import type { ApiClip, ApiVideo } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { NativeSelect } from "@/components/ui/native-select";
import { Skeleton } from "@/components/ui/skeleton";
import { Alert, AlertDescription } from "@/components/ui/alert";

export function ReviewTab({ initialVideoId, sessionId, onSessionChange }: { initialVideoId?: string; sessionId?: string; onSessionChange?: (id: string) => void }) {
  const { authToken } = useAppState();
  // The shared dashboard list has saved date/type filters. The recording
  // picker owns a complete catalog with only its visible session scope.
  const catalogKey = useMemo(() => ({ sessionId, authToken }), [sessionId, authToken]);
  const catalogFetcher = useCallback(async () => ({
    key: catalogKey,
    videos: await fetchAllVideos(sessionId ? { practice_session_id: sessionId } : {}, authToken),
  }), [catalogKey, sessionId, authToken]);
  const { state: catalogState, reload: reloadCatalog } = useFetchState(catalogFetcher);
  const catalogReady = catalogState.kind === "ready" && catalogState.data.key === catalogKey;
  const videos = useMemo(() => catalogState.kind === "ready" && catalogState.data.key === catalogKey
    ? [...catalogState.data.videos].sort((a, b) => (b.created_at ?? "").localeCompare(a.created_at ?? "")) : [], [catalogState, catalogKey]);
  const selectionScope = JSON.stringify([sessionId ?? "", initialVideoId ?? ""]);
  const [choice, setChoice] = useState({ scope: selectionScope, id: initialVideoId ?? "" });
  // Reset the current choice on navigation so Back cannot resurrect an old
  // manual choice belonging to an earlier visit to the same URL.
  if (choice.scope !== selectionScope) setChoice({ scope: selectionScope, id: initialVideoId ?? "" });
  const requestedId = choice.scope === selectionScope ? choice.id : initialVideoId ?? "";
  const selectedVideo = requestedId ? videos.find((video) => video.id === requestedId)
    : videos.find((video) => video.status === "ready") ?? videos[0];
  if (choice.scope === selectionScope && !choice.id && selectedVideo) setChoice({ scope: selectionScope, id: selectedVideo.id });
  const selectedVideoId = selectedVideo?.id ?? "";
  const missingSelection = catalogReady && Boolean(requestedId) && !selectedVideo;

  const fetcher = useCallback(async () => {
    const clips = selectedVideoId ? await fetchClipsForVideo(selectedVideoId, authToken) : [];
    return { videoId: selectedVideoId, clips };
  }, [selectedVideoId, authToken]);
  const { state, reload } = useFetchState(fetcher);

  const clips = useMemo(() => {
    if (state.kind !== "ready" || state.data.videoId !== selectedVideoId) return [];
    return [...state.data.clips].sort((a, b) => {
      const ap = a.play_number ?? Number.MAX_SAFE_INTEGER;
      const bp = b.play_number ?? Number.MAX_SAFE_INTEGER;
      if (ap !== bp) return ap - bp;
      return a.start_time - b.start_time;
    });
  }, [state, selectedVideoId]);

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHeader className="flex flex-row flex-wrap items-start justify-between gap-3">
          <div>
            <h2 className="text-base font-semibold">
              Review &amp; Tag Plays
            </h2>
            <p className="mt-0.5 max-w-2xl text-xs text-muted-foreground">
              Choose a recording to review its plays, add tags, and correct tracking. Camera views can be matched in the session workspace.
            </p>
            <p className="mt-2 text-xs text-muted-foreground" role="status" data-testid="recording-scope">
              {catalogReady ? `${videos.length} recording${videos.length === 1 ? "" : "s"}` : catalogState.kind === "error" ? "Recording list unavailable" : catalogState.kind === "offline" ? "Library disconnected" : "Loading recordings…"}
              {sessionId ? " in the selected session" : " · All uploaded recordings"}
              {". Saved dashboard date and film-type filters do not limit this list."}
            </p>
            <div className="mt-2 flex flex-wrap gap-2">
              {sessionId ? onSessionChange ? <Button variant="link" size="sm" onClick={() => onSessionChange("")}>Show all recordings</Button>
                : <Button asChild variant="link" size="sm"><Link href="/film-room/?tab=review">Show all recordings</Link></Button> : null}
              <Button variant="ghost" size="sm" onClick={() => void reloadCatalog()} disabled={catalogState.kind === "loading"}><RefreshCw className="size-3.5" /> Refresh recordings</Button>
              <Button asChild variant="link" size="sm"><Link href={sessionId ? `/film-room/?tab=browse&sessionId=${encodeURIComponent(sessionId)}` : "/film-room/?tab=upload"}>Add recordings</Link></Button>
            </div>
            <p className="mt-1 max-w-2xl text-xs text-muted-foreground">Only film uploaded to this library appears here. Files on your computer and separate labeling proposals need to be uploaded first.</p>
          </div>
          <div className="flex min-w-64 flex-col gap-1">
            <Label
              htmlFor="review-video-picker"
              className="text-xs font-semibold text-muted-foreground"
            >
              Recording
            </Label>
            <NativeSelect
              id="review-video-picker"
              value={selectedVideoId}
              onChange={(e) => setChoice({ scope: selectionScope, id: e.target.value })}
              aria-label="Video to review"
              data-testid="review-video-picker"
              disabled={videos.length === 0}
            >
              {!selectedVideoId && <option value="">{catalogReady ? videos.length ? "Choose a recording" : "No recordings in this scope" : catalogState.kind === "error" ? "Recording list unavailable" : catalogState.kind === "offline" ? "Library disconnected" : "Loading recordings…"}</option>}
              {videos.map((v) => (
                <option key={v.id} value={v.id}>
                  {videoOptionLabel(v)}
                </option>
              ))}
            </NativeSelect>
          </div>
        </CardHeader>
      </Card>

      {catalogState.kind === "error" ? <Alert variant="destructive" role="alert">
        <AlertDescription>Recordings could not be loaded. {catalogState.message}</AlertDescription>
        <Button variant="outline" size="sm" className="mt-2 w-fit" onClick={() => void reloadCatalog()}>Retry recordings</Button>
      </Alert> : null}

      <Card>
        <CardHeader>
          <h2 className="text-base font-semibold">
            Plays{selectedVideo ? ` — ${selectedVideo.filename}` : ""}
          </h2>
        </CardHeader>
        <CardContent>
          {!selectedVideoId ? (
            <p className="text-xs text-muted-foreground" data-testid="review-empty">
              {catalogState.kind === "error" ? "Recordings could not be loaded. Retry the recording list above."
                : catalogState.kind === "offline" ? "Backend offline — connect your library to load recordings."
                : !catalogReady
                ? "Loading film…"
                : missingSelection ? sessionId ? "The requested recording is not available in this scope. Choose a recording above or show all recordings." : "The requested recording is not available in this library. Choose a recording above, or add it to the library."
                : sessionId ? "No recordings in the selected session. Show all recordings or add film to this session."
                : "No film yet. Add recordings to this library; plays appear here after processing."}
            </p>
          ) : (
            <>
              {(state.kind === "loading" || state.kind === "ready" && state.data.videoId !== selectedVideoId) && (
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
              {state.kind === "ready" && state.data.videoId === selectedVideoId && clips.length === 0 && (
                <p className="text-xs text-muted-foreground" data-testid="review-no-clips">
                  No clips processed for this video yet.
                  {selectedVideo?.status === "uploaded"
                    ? " Start processing from the Upload & processing tab."
                    : selectedVideo?.status === "processing"
                      ? " Processing is still running — clips appear as play detection finishes."
                      : ""}
                </p>
              )}
              {state.kind === "ready" && state.data.videoId === selectedVideoId && clips.length > 0 && (
                <div className="flex flex-col gap-2">
                  {clips.map((clip) => (
                    <ReviewClipRow key={clip.id} clip={clip} sessionId={sessionId} />
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

function ReviewClipRow({ clip, sessionId }: { clip: ApiClip; sessionId?: string }) {
  const possession = clip.our_possession ?? clip.side_of_ball ?? null;
  const possessionLabel = possession ? POSSESSION_LABEL[possession] : null;
  const isPreliminary =
    clip.is_preliminary === true || clip.result_state === "preliminary";
  const fromParams = new URLSearchParams({ tab: "review", videoId: clip.video_id });
  if (sessionId) fromParams.set("sessionId", sessionId);
  return (
    <Link
      href={`/clip-review/?${new URLSearchParams({ clipId: clip.id, from: `/film-room/?${fromParams.toString()}` }).toString()}`}
      data-testid={`review-clip-${clip.id}`}
      className="group flex items-center justify-between gap-3 rounded-md border border-border-soft px-3 py-2.5 transition-colors hover:border-border hover:bg-accent/50"
    >
      <div className="min-w-0">
        <span className="text-sm font-semibold">
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
        <span className="inline-flex items-center gap-1 text-xs font-semibold text-primary opacity-80 transition-opacity group-hover:opacity-100">
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
