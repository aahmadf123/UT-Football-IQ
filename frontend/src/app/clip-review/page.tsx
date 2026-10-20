"use client";

import { clipView, sameplayAngles, situationLabel, viewLabel } from "@/lib/clip-view";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, memo, useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { ArrowLeft, Check, ChevronRight, Clapperboard, Crosshair, Film, Layers, Route, ScanLine, Users } from "lucide-react";
import { AnalyticsCard } from "@/components/analytics-card";
import { ClipStateBadges } from "@/components/clip-state-badge";
import { FootballShell } from "@/components/shell/app-shell";
import { useAppState } from "@/lib/app-state";
import {
  fetchClip,
  fetchClipOverlays,
  fetchClipsForVideo,
  fetchVideo,
  fetchVideoDownloadUrl,
  parseStorageUri,
  signedUrlRefreshDelay,
} from "@/lib/api";
import { pauseBackgroundRefresh, resumeBackgroundRefresh, isBackgroundRefreshPaused } from "@/lib/background-refresh";
import { createMediaClock, useMediaTime, useThrottledMediaTime, type MediaClock } from "@/lib/media-clock";
import type {
  ApiClip,
  ApiVideo,
  ClipOverlayPayload,
  OverlayLayerKey,
  OverlayTracklet,
} from "@/lib/types";
import { activeTrackPointIndex, trackingTimeSeconds } from "@/lib/tracking-time";
import { POSSESSION_LABEL, SESSION_KIND_LABEL } from "@/lib/labels";
import { CorrectionsPanel } from "./corrections-panel";
import { ReviewMetadataPanel } from "./review-metadata-panel";
import { RecordingReview } from "./recording-review";
import { CorrectionHistory } from "./correction-history";
import { FieldMinimap } from "./field-minimap";
import { OverlayCanvas, eventTimeSeconds } from "./overlay-canvas";
import { VideoTransport } from "./video-transport";
import { canSeeTechnicalDetails, resolveCurrentRole } from "@/lib/roles";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState } from "@/components/composite/empty-state";
import { StatLine } from "@/components/composite/stat-chip";
import { cn } from "@/lib/utils";
import { friendlyMessage } from "@/lib/api-error";

const LAYER_TOGGLES: ReadonlyArray<{ key: OverlayLayerKey; label: string }> = [
  { key: "raw", label: "Original film" },
  { key: "tracks", label: "Player boxes" },
  { key: "labels", label: "Identities" },
  { key: "events", label: "Events" },
  { key: "metrics", label: "Metrics" },
  { key: "field", label: "Field view" },
];

// Default playback frame rate when the parent video has no ``fps`` recorded.
// Phase CV pipeline targets 30fps; overlays sync to this by default so the
// player→frame conversion is still meaningful before metadata is filled in.
const DEFAULT_FPS = 30;

type ReviewState =
  | { kind: "loading" }
  | { kind: "offline" }
  | { kind: "error"; message: string }
  | {
      kind: "ready";
      clip: ApiClip;
      video: ApiVideo;
      playbackUrl: string | null;
      playbackUnavailable: "none" | "no_storage_uri" | "url_generation_failed";
      playbackUsesClipAsset: boolean;
    };

type OverlayState =
  | { kind: "loading" }
  | { kind: "empty"; payload: ClipOverlayPayload }
  | { kind: "ready"; payload: ClipOverlayPayload }
  | { kind: "error"; message: string };

export default function ClipReviewPage() {
  return (
    <FootballShell activePage="clip-review">
      <Suspense
        fallback={
          <div role="status" aria-label="Loading" className="flex flex-col gap-2">
            <Skeleton className="h-72 w-full" />
          </div>
        }
      >
        <ClipReviewLoader />
      </Suspense>
    </FootballShell>
  );
}

function ClipReviewLoader() {
  const searchParams = useSearchParams();
  const clipId = searchParams.get("clipId") ?? "";
  const videoId = searchParams.get("videoId") ?? "";
  if (!clipId && videoId) return <RecordingReview key={videoId} videoId={videoId} />;
  if (!clipId) {
    return (
      <EmptyState
        icon={Clapperboard}
        title="No clip selected"
        hint="Open a play from Film Room → Review plays."
        action={
          <Button asChild variant="outline">
            <Link href="/film-room/?tab=browse">
              <ArrowLeft className="size-4" /> Film Room
            </Link>
          </Button>
        }
      />
    );
  }
  return <ClipReviewView key={clipId} clipId={clipId} />;
}

function ClipReviewView({ clipId }: { clipId: string }) {
  const { authToken } = useAppState();
  const [state, setState] = useState<ReviewState>({ kind: "loading" });
  const [overlayState, setOverlayState] = useState<OverlayState>({ kind: "loading" });
  // Ordered sibling clips of the same video, for prev/next play navigation.
  const [siblingClips, setSiblingClips] = useState<ApiClip[]>([]);
  const [revision, setRevision] = useState(0);
  // "auto" plays the per-play proxy when one exists (a few MB, 30 fps, made
  // for the browser) and the full recording otherwise; an <video> error flips
  // to the other source once.
  const [sourceChoice, setSourceChoice] = useState<"auto" | "parent" | "clip">("auto");
  const [playbackError, setPlaybackError] = useState<string | null>(null);
  const refresh = useCallback(() => setRevision((current) => current + 1), []);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const clip = await fetchClip(clipId, authToken);
        const video = await fetchVideo(clip.video_id, authToken);
        let playbackUrl: string | null = null;
        let playbackUnavailable: "none" | "no_storage_uri" | "url_generation_failed" = "none";
        let playbackUsesClipAsset = false;
        try {
          const clipStorage = parseStorageUri(clip.storage_uri);
          const videoStorage = parseStorageUri(video.storage_uri);
          // Rendered previews may contain burned-in boxes, so only the clean
          // proxy replaces the original by default. Streaming the 3+ GB game
          // recording instead made game clips stall every second or two.
          const clipPlayable = clipStorage?.bucket === "clips";
          const useClip =
            sourceChoice === "clip"
              ? clipPlayable
              : sourceChoice === "parent"
                ? !videoStorage && clipPlayable
                : clipPlayable && isCleanProxy(clip);
          const target = useClip ? clipStorage : videoStorage ?? clipStorage;
          playbackUsesClipAsset = Boolean(target && target === clipStorage);
          if (!target) {
            playbackUnavailable = "no_storage_uri";
          } else {
            playbackUrl = await fetchVideoDownloadUrl(target.bucket, target.key, authToken);
            if (!playbackUrl) playbackUnavailable = "url_generation_failed";
          }
        } catch {
          playbackUnavailable = "url_generation_failed";
        }
        if (cancelled) return;
        setState({ kind: "ready", clip, video, playbackUrl, playbackUnavailable, playbackUsesClipAsset });
        if (playbackUrl) setPlaybackError(null);
        // Prev/next-play navigation degrades to disabled buttons on failure.
        try {
          const clips = await siblingClipsFor(clip.video_id, authToken);
          if (!cancelled) setSiblingClips(clips);
        } catch {
          if (!cancelled) setSiblingClips([]);
        }
      } catch (err) {
        if (cancelled) return;
        setState({
          kind: "error",
          message: friendlyMessage(err),
        });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [clipId, authToken, revision, sourceChoice]);

  // Signed URLs last an hour: re-mint shortly before expiry so a long review
  // session never breaks mid-play (the player resumes at the same moment).
  const playbackTarget = state.kind === "ready" ? playbackStorage(state) : null;
  const targetBucket = playbackTarget?.bucket ?? null;
  const targetKey = playbackTarget?.key ?? null;
  useEffect(() => {
    if (!targetBucket || !targetKey) return;
    const timer = window.setTimeout(async () => {
      const url = await fetchVideoDownloadUrl(targetBucket, targetKey, authToken, { fresh: true });
      if (url) setState((current) => (current.kind === "ready" ? { ...current, playbackUrl: url } : current));
    }, signedUrlRefreshDelay(targetBucket, targetKey));
    return () => window.clearTimeout(timer);
  }, [targetBucket, targetKey, authToken]);

  // Overlay fetch is independent of the clip fetch — a clip with no overlays
  // (e.g. ingest still running) should still render the player + metadata.
  useEffect(() => {
    let cancelled = false;
    let signature = "";
    const load = async () => {
      try {
        const payload = await fetchClipOverlays(clipId, authToken);
        if (cancelled) return;
        // Replacing an identical payload re-renders every overlay consumer.
        const next = overlaySignature(payload);
        if (next === signature) return;
        signature = next;
        const empty =
          !payload.layers_available.tracklets &&
          !payload.layers_available.events &&
          !payload.layers_available.labels &&
          !payload.layers_available.metrics;
        setOverlayState(empty ? { kind: "empty", payload } : { kind: "ready", payload });
      } catch (err) {
        if (cancelled) return;
        setOverlayState({
          kind: "error",
          message: friendlyMessage(err),
        });
      }
    };
    void load();
    const interval = window.setInterval(() => {
      if (document.visibilityState !== "hidden" && !isBackgroundRefreshPaused()) void load();
    }, 5000);
    return () => {
      cancelled = true;
      window.clearInterval(interval);
    };
  }, [clipId, authToken, revision]);

  if (state.kind === "loading") {
    return (
      <div role="status" aria-busy="true" aria-label="Loading clip metadata" className="flex flex-col gap-2">
        <p className="text-xs text-muted-foreground">Loading clip metadata…</p>
        <Skeleton className="h-72 w-full" />
      </div>
    );
  }
  if (state.kind === "offline") {
    return (
      <EmptyState
        icon={Clapperboard}
        title="Clip Review is unavailable offline"
        hint="Playback, overlays, and corrections appear when the team server is connected."
        action={
          <Button asChild variant="outline">
            <Link href="/film-room/?tab=browse">
              <ArrowLeft className="size-4" /> Back to Film Room
            </Link>
          </Button>
        }
      />
    );
  }
  if (state.kind === "error") {
    return (
      <Alert variant="destructive" role="alert">
        <AlertTitle>Could not load clip</AlertTitle>
        <AlertDescription>{state.message}</AlertDescription>
        <Button variant="outline" size="sm" onClick={refresh}>Try again</Button>
        <Button asChild variant="outline" size="sm" className="mt-2 w-fit">
          <Link href="/film-room/?tab=browse">
            <ArrowLeft className="size-3.5" /> Back to Film Room
          </Link>
        </Button>
      </Alert>
    );
  }

  return <ClipReviewReady state={state} overlayState={overlayState} siblingClips={siblingClips} onRefresh={refresh} playbackError={playbackError} onPlaybackError={() => {
    const clipPlayable = parseStorageUri(state.clip.storage_uri)?.bucket === "clips";
    const parentPlayable = Boolean(parseStorageUri(state.video.storage_uri));
    if (state.playbackUsesClipAsset && parentPlayable && sourceChoice !== "parent") {
      setSourceChoice("parent");
      setPlaybackError(null);
    } else if (!state.playbackUsesClipAsset && clipPlayable && sourceChoice !== "clip") {
      setSourceChoice("clip");
      setPlaybackError(null);
    } else setPlaybackError("Playback failed. Refresh its link, or reprocess this recording to create a browser-compatible clean clip.");
  }} />;
}

function ClipReviewReady({
  state,
  overlayState,
  siblingClips,
  onRefresh,
  playbackError,
  onPlaybackError,
}: {
  state: Extract<ReviewState, { kind: "ready" }>;
  overlayState: OverlayState;
  siblingClips: ApiClip[];
  onRefresh: () => void;
  playbackError: string | null;
  onPlaybackError: () => void;
}) {
  const router = useRouter();
  const { authToken, data } = useAppState();
  const { clip, video, playbackUrl, playbackUnavailable, playbackUsesClipAsset } = state;
  const showTechnicalDetails = canSeeTechnicalDetails(resolveCurrentRole(authToken));
  // Tracklet selected by clicking its box on the overlay; prefills corrections.
  // Stored as a fresh object per click so re-selecting the same tracklet still
  // re-triggers the corrections panel (e.g. after the coach hid it).
  const [selection, setSelection] = useState<{ trackletId: string } | null>(null);
  const [sideTab, setSideTab] = useState<SideTab>("tag");
  // A box clicked on the film opens the correction form; a pick from the
  // Players list stays there to show that player's details.
  const selectTracklet = useCallback((trackletId: string) => {
    setSelection({ trackletId });
    setSideTab("tag");
  }, []);
  const inspectTracklet = useCallback((trackletId: string) => setSelection({ trackletId }), []);
  const clearSelection = useCallback(() => setSelection(null), []);
  const possession = clip.our_possession ?? clip.side_of_ball ?? video.our_possession ?? null;
  const possessionLabel = possession ? POSSESSION_LABEL[possession] : null;
  const sessionKindLabel = clip.session_kind
    ? SESSION_KIND_LABEL[clip.session_kind]
    : video.session_kind
      ? SESSION_KIND_LABEL[video.session_kind]
      : "Session";

  const overlayPayload = overlayState.kind === "ready" || overlayState.kind === "empty"
    ? overlayState.payload
    : null;
  const sourceFps = overlayPayload?.source_fps ?? video.fps;
  const fps = sourceFps != null && sourceFps > 0 ? sourceFps : DEFAULT_FPS;
  const sourceStartTime = overlayPayload?.source_start_time_seconds ?? clip.start_time;
  const [showTrails, setShowTrails] = useState(true);
  const [intrinsicSize, setIntrinsicSize] = useState<{ width: number; height: number } | null>(null);
  const [activeLayers, setActiveLayers] = useState<Set<OverlayLayerKey>>(
    () => new Set<OverlayLayerKey>(["tracks", "events", "labels", "metrics"]),
  );
  // Playback time lives in a media clock, not React state: per-frame state
  // re-rendered this whole page 30-60x a second. Only the overlay subscribes
  // per frame; other time readers are throttled (see <Timed>).
  // Parent-video playback uses full-video time and must be shifted into the
  // clip timeline. Clip-asset playback already starts at clip-local time 0.
  const clock = useMemo(() => createMediaClock(), []);
  const toLocal = useCallback(
    (elementTime: number) =>
      playbackUsesClipAsset ? Math.max(0, elementTime) : Math.max(0, elementTime - clip.start_time),
    [playbackUsesClipAsset, clip.start_time],
  );

  const videoRef = useRef<HTMLVideoElement | null>(null);
  const onTimeUpdate = useCallback(() => {
    const el = videoRef.current;
    if (el) clock.set(el.currentTime);
  }, [clock]);
  useEffect(() => {
    const element = videoRef.current;
    if (!element || !element.requestVideoFrameCallback) return;
    let handle = 0;
    const frame = (_now: number, metadata: VideoFrameCallbackMetadata) => {
      clock.set(metadata.mediaTime);
      handle = element.requestVideoFrameCallback(frame);
    };
    handle = element.requestVideoFrameCallback(frame);
    return () => element.cancelVideoFrameCallback(handle);
  }, [playbackUrl, clock]);

  // No background polling while film plays; resume on pause, end or leave.
  // A re-minted URL (expiry refresh) resumes at the same moment and state.
  const resumeRef = useRef<{ time: number; playing: boolean } | null>(null);
  useEffect(() => {
    const el = videoRef.current;
    if (!el) return;
    const reason = `clip-review-${clip.id}`;
    const onPlay = () => pauseBackgroundRefresh(reason);
    const onStop = () => resumeBackgroundRefresh(reason);
    el.addEventListener("play", onPlay);
    el.addEventListener("pause", onStop);
    el.addEventListener("ended", onStop);
    return () => {
      resumeRef.current = { time: el.currentTime, playing: !el.paused };
      el.removeEventListener("play", onPlay);
      el.removeEventListener("pause", onStop);
      el.removeEventListener("ended", onStop);
      resumeBackgroundRefresh(reason);
    };
  }, [playbackUrl, clip.id]);

  // Prev/next play: through a playlist when one was opened (e.g. "3rd-down
  // conversions" from Team Stats), else the same video by start time.
  const searchParams = useSearchParams();
  const playlistParam = searchParams.get("playlist") ?? "";
  const playlist = useMemo(
    () => (playlistParam ? playlistParam.split(",").filter(Boolean) : []),
    [playlistParam],
  );
  const playlistIndex = playlist.indexOf(clip.id);
  // Back returns where the coach came from (a review list, a Team Stats
  // playlist); only same-site paths are honoured.
  const fromParam = searchParams.get("from") ?? "";
  const backHref = fromParam.startsWith("/") && !fromParam.startsWith("//")
    ? fromParam
    : `/film-room/?tab=review&videoId=${encodeURIComponent(video.id)}`;
  const sequence = playlistIndex >= 0 ? playlist : siblingClips.map((c) => c.id);
  const clipIndex = playlistIndex >= 0 ? playlistIndex : siblingClips.findIndex((c) => c.id === clip.id);
  const prevClipId = clipIndex > 0 ? sequence[clipIndex - 1] : null;
  const nextClipId = clipIndex >= 0 && clipIndex < sequence.length - 1 ? sequence[clipIndex + 1] : null;
  const goToClip = useCallback(
    (id: string) => {
      const params = new URLSearchParams({ clipId: id });
      if (playlistIndex >= 0) params.set("playlist", playlistParam);
      router.push(`/clip-review/?${params.toString()}`);
    },
    [router, playlistIndex, playlistParam],
  );
  const goPrevClip = useMemo(
    () => (prevClipId ? () => goToClip(prevClipId) : null),
    [prevClipId, goToClip],
  );
  const goNextClip = useMemo(
    () => (nextClipId ? () => goToClip(nextClipId) : null),
    [nextClipId, goToClip],
  );

  // Clip-local seek target shared by the transport and the timeline.
  const clipOffset = playbackUsesClipAsset ? 0 : clip.start_time;
  const clipDuration = Math.max(clip.end_time - clip.start_time, 0);
  const clipBounds = useMemo(
    () => ({ start: clip.start_time, end: clip.end_time, duration: video.duration_seconds ?? null }),
    [clip.start_time, clip.end_time, video.duration_seconds],
  );
  const seekToLocal = useCallback(
    (t: number) => {
      const el = videoRef.current;
      if (!el) return;
      el.currentTime = clipOffset + Math.min(Math.max(t, 0), Math.max(clipDuration - 0.01, 0));
    },
    [clipOffset, clipDuration],
  );

  const toggleLayer = useCallback((key: OverlayLayerKey) => {
    setActiveLayers((prev) => {
      const next = new Set(prev);
      if (key === "raw") {
        // ``raw`` is mutually exclusive — it clears every overlay layer.
        return new Set<OverlayLayerKey>(prev.has("raw") ? ["tracks", "events", "labels", "metrics"] : ["raw"]);
      }
      if (next.has("raw")) next.delete("raw");
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }, []);

  const trackTimeAt = useCallback(
    (localTime: number) => trackingTimeSeconds(overlayPayload, localTime, clip.start_time),
    [overlayPayload, clip.start_time],
  );

  // Instant next play: warm the browser cache with the next clip's proxy
  // while this one plays (the URL is reused, so the cache hit is real).
  usePreloadClip(nextClipId, authToken);

  // Roster names for attributed tracklets ("#7 · C. Jones" on the Labels layer
  // and the field mini-map).
  const playerNamesById = useMemo(() => {
    const map = new Map<string, string>();
    for (const p of data.players) map.set(p.id, `#${p.jersey} ${p.name}`);
    return map;
  }, [data.players]);

  // Per-tracklet speed callouts from the coach-visible metrics payload.
  const metricTextByTrackletId = useMemo(() => {
    const map = new Map<string, string>();
    if (!overlayPayload) return map;
    for (const m of overlayPayload.metrics) {
      if (m.tracklet_id == null || m.metric_name !== "max_speed") continue;
      const yps = m.metric_value["yards_per_second"];
      if (typeof yps === "number") map.set(m.tracklet_id, `Max ${yps.toFixed(1)} yd/s`);
    }
    return map;
  }, [overlayPayload]);

  const overlayLayersForCanvas = useMemo(() => {
    if (activeLayers.has("raw")) return new Set<OverlayLayerKey>();
    return activeLayers;
  }, [activeLayers]);

  const eventsForTimeline = useMemo(() => {
    if (!overlayPayload) return [];
    return overlayPayload.events
      .map((e) => ({ event: e, t: eventTimeSeconds(e, fps, sourceStartTime) }))
      .filter((row): row is { event: typeof row.event; t: number } => row.t != null && row.t >= 0 && row.t <= clipDuration)
      .sort((a, b) => a.t - b.t);
  }, [overlayPayload, fps, sourceStartTime, clipDuration]);
  const eventTimesSorted = useMemo(
    () => eventsForTimeline.map((row) => row.t).sort((a, b) => a - b),
    [eventsForTimeline],
  );

  // Calibration banner (explained suppression). Dismissal is per-clip so
  // navigating to a different clip resurfaces the notice.
  const calibration = overlayPayload?.calibration ?? null;
  const [calibrationDismissedFor, setCalibrationDismissedFor] = useState<string | null>(null);
  const dismissCalibrationBanner = useCallback(
    () => setCalibrationDismissedFor(clip.id),
    [clip.id],
  );

  return (
    <div className="flex flex-col gap-5" data-testid="review-workspace">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <nav aria-label="Breadcrumb" className="mb-2 flex items-center gap-2 text-[0.65rem] font-semibold uppercase tracking-[0.18em] text-muted-foreground"><Film className="size-3.5 text-primary" /> <Link href="/film-room/" className="hover:text-foreground">Film room</Link> <ChevronRight className="size-3" /> <Link href={`/film-room/?tab=review&videoId=${encodeURIComponent(video.id)}`} className="hover:text-foreground">Review plays</Link> <ChevronRight className="size-3" /> <span aria-current="page">Play</span></nav>
          <h2 className="max-w-3xl break-words text-xl font-semibold tracking-tight">{video.filename}</h2>
          <p className="mt-1 text-sm text-muted-foreground">{video.camera_label || "Camera view"} · {sessionKindLabel}{clipIndex >= 0 && sequence.length ? ` · ${playlistIndex >= 0 ? "Playlist play" : "Play"} ${clipIndex + 1} of ${sequence.length}` : ""}</p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button asChild variant="ghost" size="sm"><Link href={`/clip-review/?videoId=${encodeURIComponent(video.id)}`}>Watch full recording</Link></Button>
          <Button asChild variant="outline" size="sm"><Link href={backHref}><ArrowLeft className="size-3.5" /> Back</Link></Button>
        </div>
      </div>

      <div className="grid items-start gap-5 lg:grid-cols-[minmax(0,1fr)_300px] xl:grid-cols-[minmax(0,1fr)_330px] 2xl:grid-cols-[minmax(0,1fr)_360px]">
      <Card className="min-w-0 gap-0 overflow-hidden border-border-soft py-0 shadow-none">
        <CardContent className="px-0">
          <div className="border-b border-border-soft px-5 py-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <div className="flex flex-wrap items-center gap-2">
                <h3 className="text-lg font-semibold tracking-tight">
                  {clip.play_number != null
                    ? `Play #${clip.play_number}`
                    : `Clip ${clip.id.slice(0, 8)}`}
                </h3>
                <ClipStateBadges
                  isPreliminary={clip.is_preliminary}
                  reviewState={clip.review_state}
                />
                <AngleSwitch
                  clip={clip}
                  siblings={siblingClips}
                  playlist={playlist}
                  playlistIndex={playlistIndex}
                />
              </div>
              {situationLabel(clip) ? (
                <p className="mt-1 font-mono text-xs font-semibold text-foreground">{situationLabel(clip)}</p>
              ) : null}
              <p className="mt-1 text-xs text-muted-foreground">
                {sessionKindLabel}
                {video.opponent_team ? ` · vs. ${video.opponent_team}` : ""}
                {possessionLabel ? ` · ${possessionLabel}` : ""}
              </p>
            </div>
            <div className="flex items-center gap-3 text-xs text-muted-foreground"><span className="inline-flex items-center gap-1.5"><Users className="size-3.5" /><strong className="font-mono text-foreground">{overlayPayload?.tracklets.length ?? 0}</strong> tracks</span><span className="h-4 border-l border-border-soft" /><span className="font-mono">{clipDuration.toFixed(1)}s</span></div>
          </div>
          </div>

          <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border-soft bg-secondary/20 px-4 py-2.5">
            <div className="flex items-center gap-2 text-xs"><ScanLine className="size-4 text-primary" /><span className="font-semibold">{activeLayers.has("raw") ? "Original film" : "Vision overlays"}</span><span className="text-muted-foreground">· {video.camera_label || "Camera view"}</span></div>
            <span className="max-w-full truncate text-xs text-muted-foreground" title={video.filename}>{video.filename}</span>
          </div>

          <div className="relative flex aspect-video min-h-56 items-center justify-center overflow-hidden bg-black [&:fullscreen]:h-screen [&:fullscreen]:w-screen [&:fullscreen_video]:h-full" style={{ aspectRatio: intrinsicSize ? `${intrinsicSize.width} / ${intrinsicSize.height}` : undefined }}>
            {playbackUrl ? (
              <>
                <video
                  ref={videoRef}
                  src={playbackUrl}
                  playsInline
                  onTimeUpdate={onTimeUpdate}
                  onSeeked={onTimeUpdate}
                  onError={onPlaybackError}
                  onLoadedMetadata={() => {
                    // Parent-video playback loads at t=0, before the clip —
                    // jump to the clip start once metadata makes seeking safe.
                    const el = videoRef.current;
                    if (el && el.videoWidth > 0 && el.videoHeight > 0) setIntrinsicSize({ width: el.videoWidth, height: el.videoHeight });
                    const resume = resumeRef.current;
                    resumeRef.current = null;
                    if (el && resume && resume.time > 0) {
                      el.currentTime = resume.time;
                      if (resume.playing) void el.play().catch(() => undefined);
                    } else if (el && clipOffset > 0 && el.currentTime < clipOffset) {
                      el.currentTime = clipOffset;
                    }
                    if (el) clock.set(el.currentTime);
                  }}
                  onClick={() => {
                    const el = videoRef.current;
                    if (!el) return;
                    if (el.paused) {
                      if (el.currentTime >= clipOffset + clipDuration - 0.06) el.currentTime = clipOffset;
                      void el.play().catch(() => undefined);
                    }
                    else el.pause();
                  }}
                  aria-label={`Clip ${clip.play_number ?? clip.id} video`}
                  preload="auto"
                  className="absolute inset-0 block h-full w-full cursor-pointer object-contain"
                />
                <FrameOverlay
                  clock={clock}
                  toLocal={toLocal}
                  trackTimeAt={trackTimeAt}
                  overlayPayload={overlayPayload}
                  overlayLoading={overlayState.kind === "loading"}
                  fps={fps}
                  sourceStartTime={sourceStartTime}
                  clipDuration={clipDuration}
                  videoWidth={video.width ?? intrinsicSize?.width ?? null}
                  videoHeight={video.height ?? intrinsicSize?.height ?? null}
                  activeLayers={overlayLayersForCanvas}
                  rawOnly={activeLayers.has("raw")}
                  playerNamesById={playerNamesById}
                  metricTextByTrackletId={metricTextByTrackletId}
                  selectedTrackletId={selection?.trackletId ?? null}
                  onSelectTracklet={selectTracklet}
                  showTrails={showTrails}
                />
              </>
            ) : (
              <div className="p-6 text-center text-muted-foreground">
                <p className="m-0 text-sm font-semibold">Video not available</p>
                <p className="mt-2 text-xs">
                  {playbackUnavailable === "no_storage_uri"
                    ? "No storage URI found. The video may not have been uploaded or rendered yet."
                    : "Video playback failed or is unavailable. The signed URL may have expired or been rejected, or the file may be missing from storage."}
                </p>
              </div>
            )}
          </div>

          <div className="px-4 pb-4 pt-2">
          {playbackUrl && (
            <Timed clock={clock} everyMs={100}>
              {(t) => (
                <VideoTransport
                  videoRef={videoRef}
                  fps={fps}
                  clipOffset={clipOffset}
                  duration={clipDuration}
                  currentLocalTime={toLocal(t)}
                  eventTimes={eventTimesSorted}
                  enforceBounds={!playbackUsesClipAsset}
                  onPrevClip={goPrevClip}
                  onNextClip={goNextClip}
                />
              )}
            </Timed>
          )}
          {playbackError && <div role="alert" className="mt-2 text-sm text-status-danger">{playbackError}<Button size="sm" variant="outline" className="ml-2" onClick={onRefresh}>Refresh playback</Button></div>}

          <OverlayToggles
            active={activeLayers}
            overlayState={overlayState}
            onToggle={toggleLayer}
            showTrails={showTrails}
            onToggleTrails={() => setShowTrails((value) => !value)}
          />

          {playbackUrl && (
            <Timed clock={clock} everyMs={250}>
              {(t) => (
                <EventTimeline
                  events={eventsForTimeline}
                  clipDuration={clipDuration}
                  currentTime={toLocal(t)}
                  onSeek={seekToLocal}
                />
              )}
            </Timed>
          )}

          {calibration && calibration.analytics_safe === false && calibrationDismissedFor !== clip.id ? <CalibrationBanner reason={calibration.reason} onDismiss={dismissCalibrationBanner} /> : null}
          {overlayPayload && (overlayPayload.warnings?.length ?? 0) > 0 ? <div role="status" className="mt-3 rounded-lg border border-status-warn/30 bg-status-warn/5 p-3 text-xs"><p className="font-semibold text-status-warn">Analysis availability</p>{overlayPayload.warnings?.map((warning) => <p key={warning} className="mt-1 text-muted-foreground">{warning}</p>)}</div> : null}

          {overlayPayload && activeLayers.has("field") && !activeLayers.has("raw") && (
            <Timed clock={clock} everyMs={66}>
              {(t) => (
                <FieldMinimap
                  tracklets={overlayPayload.tracklets}
                  currentFrame={Math.round(trackTimeAt(toLocal(t)) * fps)}
                  currentTimeSeconds={trackTimeAt(toLocal(t))}
                  fps={fps}
                  calibration={overlayPayload.calibration ?? null}
                  playerNamesById={playerNamesById}
                />
              )}
            </Timed>
          )}
          </div>
        </CardContent>
      </Card>

      <aside
        aria-label="Play review tools"
        className="flex min-w-0 flex-col gap-3 lg:sticky lg:top-20 lg:max-h-[calc(100vh-6rem)]"
      >
        <div role="tablist" aria-label="Review panels" className="grid grid-cols-4 gap-1 rounded-lg border border-border-soft bg-secondary/40 p-1">
          {SIDE_TABS.map((tab) => (
            <button
              key={tab.key}
              type="button"
              role="tab"
              aria-selected={sideTab === tab.key}
              onClick={() => setSideTab(tab.key)}
              className={cn(
                "min-h-8 rounded-md px-2 text-xs font-semibold transition-colors",
                sideTab === tab.key
                  ? "bg-gradient-to-b from-primary to-gold-strong text-primary-foreground"
                  : "text-muted-foreground hover:bg-accent hover:text-foreground",
              )}
            >
              {tab.label}
              {tab.key === "plays" && sequence.length ? <span className="ml-1 font-mono font-normal opacity-80">{sequence.length}</span> : null}
            </button>
          ))}
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto pr-0.5">
        {sideTab === "tag" && (
          <Card className="gap-0 border-border-soft py-4 shadow-none">
            <CardContent className="px-4">
          <ReviewMetadataPanel key={`${clip.id}-${clip.annotation_revision ?? 0}`} clip={clip} onSaved={onRefresh} onNext={goNextClip} />

              <Timed clock={clock} everyMs={500}>
                {(t) => (
                  <CorrectionsPanel
                    key={clip.id}
                    clipId={clip.id}
                    tracklets={overlayPayload?.tracklets ?? EMPTY_TRACKLETS}
                    selection={selection}
                    annotationRevision={clip.annotation_revision}
                    clipBounds={clipBounds}
                    onSaved={onRefresh}
                    currentTimeSeconds={toLocal(t)}
                  />
                )}
              </Timed>
              <CorrectionHistory clipId={clip.id} annotationRevision={clip.annotation_revision} />
              <OverlaySummary
                overlayState={overlayState}
                showMetrics={activeLayers.has("metrics") && !activeLayers.has("raw")}
                showLabels={activeLayers.has("labels") && !activeLayers.has("raw")}
              />

            </CardContent>
          </Card>
        )}
        {sideTab === "plays" && (
          <PlayQueue clips={siblingClips} currentClipId={clip.id} playlist={playlistIndex >= 0 ? playlist : null} playlistParam={playlistParam} />
        )}
        {sideTab === "players" && (
          <Timed clock={clock} everyMs={250}>
            {(t) => <TrackingInspector tracklets={overlayPayload?.tracklets ?? []} playerNamesById={playerNamesById} selectedTrackletId={selection?.trackletId ?? null} currentTimeSeconds={trackTimeAt(toLocal(t))} fps={fps} onSelect={inspectTracklet} onClear={clearSelection} loading={overlayState.kind === "loading"} />}
          </Timed>
        )}
        {sideTab === "details" && (
          <Card className="gap-0 border-border-soft py-4 shadow-none">
            <CardContent className="px-4">
              <h3 className="text-sm font-semibold">Play details</h3>
          <div className="mt-3 flex flex-col gap-1.5">
            <StatLine label="Video" value={video.filename} />
            <StatLine label="Video status" value={video.status} />
            <StatLine label="Session" value={sessionKindLabel} />
            {overlayPayload && <StatLine label="Analysis source" value={overlayPayload.provenance?.source === "real" ? "Vision model output" : overlayPayload.provenance?.source === "manual" ? "Coach annotation" : overlayPayload.provenance?.source === "heuristic" ? "Estimated from video" : "Quality not verified"} />}
            {video.opponent_team && <StatLine label="Opponent" value={video.opponent_team} />}
            {possessionLabel && <StatLine label="Possession" value={possessionLabel} />}
            {clip.play_number != null && (
              <StatLine label="Play #" value={String(clip.play_number)} />
            )}
            <StatLine
              label="Boundaries"
              value={`${clip.start_time.toFixed(1)}s → ${clip.end_time.toFixed(1)}s`}
            />
            {clip.confidence != null && (
              <StatLine label="Confidence" value={`${Math.round(clip.confidence * 100)}%`} />
            )}
            <StatLine
              label="Reviewed"
              value={
                clip.is_reviewed === true
                  ? "Yes"
                  : clip.is_reviewed === false
                    ? "No"
                    : "Unknown"
              }
            />
            {clip.result_state && (
              <StatLine
                label="Results"
                value={clip.is_preliminary ? "Preliminary (same-session)" : "Final (nightly)"}
              />
            )}
            {video.recorded_at && (
              <StatLine label="Recorded" value={new Date(video.recorded_at).toLocaleString()} />
            )}
          </div>


          {showTechnicalDetails && (
            <details className="mt-4">
              <summary className="cursor-pointer font-display text-xs font-semibold uppercase tracking-widest text-muted-foreground">
                Technical details
              </summary>
              <p className="mt-1 break-all font-mono text-[0.68rem] text-muted-foreground/80">
                {clip.storage_uri ?? "Clip not yet rendered to storage."}
              </p>
            </details>
          )}
            </CardContent>
          </Card>
        )}
        </div>
      </aside>
      </div>
    </div>
  );
}

const SIDE_TABS = [
  { key: "tag", label: "Tag" },
  { key: "plays", label: "Plays" },
  { key: "players", label: "Players" },
  { key: "details", label: "Details" },
] as const;
type SideTab = (typeof SIDE_TABS)[number]["key"];

/** Sideline | End zone toggle between the camera angles of one play. */
const AngleSwitch = memo(function AngleSwitch({
  clip,
  siblings,
  playlist,
  playlistIndex,
}: {
  clip: ApiClip;
  siblings: ApiClip[];
  playlist: string[];
  playlistIndex: number;
}) {
  const angles = sameplayAngles(clip, siblings);
  const current = clipView(clip);
  if (!current) return null;
  if (angles.length < 2) {
    return <span className="rounded-md border border-border-soft px-2 py-0.5 text-[0.65rem] font-medium text-muted-foreground">{viewLabel(current)}</span>;
  }
  return <div role="group" aria-label="Camera angle" className="inline-flex overflow-hidden rounded-md border border-border-soft text-[0.65rem] font-medium">
    {angles.map((angle) => {
      const view = clipView(angle)!;
      const active = angle.id === clip.id;
      const params = new URLSearchParams({ clipId: angle.id });
      if (playlistIndex >= 0) {
        const anglePlaylist = [...playlist];
        anglePlaylist[playlistIndex] = angle.id;
        params.set("playlist", anglePlaylist.join(","));
      }
      return <Link key={angle.id} href={`/clip-review/?${params.toString()}`} aria-current={active ? "true" : undefined} className={cn("px-2.5 py-1 transition-colors", active ? "bg-primary text-primary-foreground" : "hover:bg-accent/50")}>{viewLabel(view)}</Link>;
    })}
  </div>;
});

const PlayQueue = memo(function PlayQueue({
  clips,
  currentClipId,
  playlist,
  playlistParam,
}: {
  clips: ApiClip[];
  currentClipId: string;
  playlist: string[] | null;
  playlistParam: string;
}) {
  const currentRef = useRef<HTMLAnchorElement | null>(null);
  useEffect(() => {
    currentRef.current?.scrollIntoView({ block: "nearest" });
  }, [currentClipId]);
  const byId = useMemo(() => new Map(clips.map((item) => [item.id, item])), [clips]);
  const items = playlist ? playlist.map((id) => byId.get(id)).filter((item): item is ApiClip => Boolean(item)) : clips;
  if (items.length < 2) {
    return <p className="rounded-lg border border-dashed border-border-soft p-4 text-xs text-muted-foreground">This is the only play in this recording.</p>;
  }
  return <nav aria-label="Plays in this recording" className="rounded-lg border border-border-soft bg-secondary/20 p-3">
    <div className="mb-2 flex items-center justify-between gap-2"><h3 className="flex items-center gap-2 text-xs font-semibold"><Film className="size-3.5 text-primary" /> {playlist ? "Playlist" : "Play sequence"}</h3><span className="text-[0.65rem] text-muted-foreground">{items.length} plays · [ / ] keys</span></div>
    <ol className="flex flex-col gap-1">{items.map((item, index) => {
      const current = item.id === currentClipId;
      const params = new URLSearchParams({ clipId: item.id });
      if (playlist) params.set("playlist", playlistParam);
      return <li key={item.id}><Link ref={current ? currentRef : undefined} href={`/clip-review/?${params.toString()}`} aria-current={current ? "page" : undefined} className={cn("flex items-center justify-between gap-2 rounded-md border px-2.5 py-2 transition-colors", current ? "border-primary/60 bg-primary/10" : "border-transparent hover:border-border-soft hover:bg-accent/40")}>
        <span className="min-w-0"><span className={cn("block text-xs font-semibold", current && "text-primary")}>Play {item.play_number ?? index + 1}{clipView(item) ? <span className="ml-1 font-normal text-muted-foreground">· {viewLabel(clipView(item)!)}</span> : null}</span><span className="mt-0.5 block truncate font-mono text-[0.65rem] text-muted-foreground">{situationLabel(item) ?? `${item.start_time.toFixed(1)}–${item.end_time.toFixed(1)}s`}</span></span>
        {item.is_reviewed ? <Check aria-label="Reviewed" className="size-3.5 shrink-0 text-status-ok" /> : <span className="size-1.5 shrink-0 rounded-full bg-muted-foreground/40" />}
      </Link></li>;
    })}</ol>
  </nav>;
});

function TrackingInspector({ tracklets, playerNamesById, selectedTrackletId, currentTimeSeconds, fps, onSelect, onClear, loading }: {
  tracklets: OverlayTracklet[];
  playerNamesById: ReadonlyMap<string, string>;
  selectedTrackletId: string | null;
  currentTimeSeconds: number;
  fps: number;
  onSelect: (id: string) => void;
  onClear: () => void;
  loading: boolean;
}) {
  const [teamFilter, setTeamFilter] = useState("all");
  const selected = tracklets.find((track) => track.id === selectedTrackletId);
  const currentIndex = selected ? activeTrackPointIndex(selected, currentTimeSeconds, fps) : -1;
  const currentPoint = selected?.track_points[currentIndex];
  const selectedIndex = selected ? tracklets.indexOf(selected) : -1;
  const playerName = selected?.player_id ? playerNamesById.get(selected.player_id) : null;
  return <Card className="gap-0 overflow-hidden border-border-soft py-0 shadow-none" data-testid="tracking-inspector">
    <div className="flex items-center justify-between border-b border-border-soft px-4 py-3.5"><h3 className="flex items-center gap-2 text-sm font-semibold"><Crosshair className="size-4 text-primary" /> Player tracking</h3><span className="rounded-md bg-secondary px-2 py-0.5 font-mono text-xs text-muted-foreground">{tracklets.length}</span></div>
    <div className="p-4">
      {selected ? <div className="mb-4 rounded-lg border border-primary/25 bg-primary/5 p-3" data-testid="selected-player-inspector">
        <div className="flex items-center justify-between gap-2"><p className="text-[0.6rem] font-semibold uppercase tracking-[0.16em] text-primary">Selected player · T{selectedIndex + 1}</p><button type="button" onClick={onClear} className="rounded px-1.5 py-1 text-[0.65rem] text-muted-foreground hover:bg-secondary hover:text-foreground" aria-label="Clear selected player">Clear</button></div>
        <h4 className="mt-1 text-base font-semibold">{playerName ?? "Unassigned player"}</h4>
        <p className="mt-1 text-xs text-muted-foreground">{selected.team_label === "home" ? "Toledo" : selected.team_label === "away" ? "Opponent" : "Team not assigned"}{selected.position_group ? ` · ${selected.position_group}` : ""}</p>
        <div className="mt-3 grid grid-cols-2 gap-3 border-t border-primary/15 pt-3"><div><p className="text-[0.65rem] text-muted-foreground">Track confidence</p><p className="mt-1 font-mono text-sm">{selected.track_confidence != null ? `${Math.round(selected.track_confidence * 100)}%` : "Unverified"}</p></div><div><p className="text-[0.65rem] text-muted-foreground">Current frame</p><p className={cn("mt-1 text-xs font-medium", currentPoint?.bbox ? "text-status-ok" : "text-muted-foreground")}>{currentPoint?.bbox ? "Player visible" : "Outside track"}</p></div></div>
        <p className="mt-3 text-[0.65rem] leading-relaxed text-muted-foreground">Use the correction form below to assign a jersey, team, or roster identity.</p>
      </div> : <p className="mb-4 text-xs leading-relaxed text-muted-foreground">Select a player box on the film or a track below to inspect and correct its identity.</p>}
      {loading ? <p role="status" className="text-xs text-muted-foreground">Loading player tracks…</p> : tracklets.length ? <>
        <div role="group" aria-label="Filter player tracks by team" className="mb-3 flex gap-1 rounded-md bg-background/40 p-1">{[{ id: "all", label: "All" }, { id: "home", label: "Toledo" }, { id: "away", label: "Opponent" }].map((filter) => <button key={filter.id} type="button" aria-pressed={teamFilter === filter.id} onClick={() => setTeamFilter(filter.id)} className={cn("min-h-8 flex-1 rounded px-2 text-[0.7rem] font-medium transition-colors", teamFilter === filter.id ? "bg-secondary text-foreground" : "text-muted-foreground hover:text-foreground")}>{filter.label}</button>)}</div>
        <div className="max-h-60 space-y-1 overflow-y-auto" aria-label="Player track list">{tracklets.map((track, index) => {
          if (teamFilter !== "all" && track.team_label !== teamFilter) return null;
          const pointIndex = activeTrackPointIndex(track, currentTimeSeconds, fps);
          const visible = pointIndex >= 0 && track.track_points[pointIndex]?.bbox != null;
          const name = track.player_id ? playerNamesById.get(track.player_id) : null;
          return <button key={track.id} type="button" onClick={() => onSelect(track.id)} aria-pressed={selectedTrackletId === track.id} aria-label={`Inspect ${name ?? `track T${index + 1}`}`} className={cn("flex w-full items-center gap-2.5 rounded-md border px-2.5 py-2 text-left transition-colors", selectedTrackletId === track.id ? "border-primary/35 bg-primary/10" : "border-transparent hover:border-border-soft hover:bg-secondary/50")}>
            <span className={cn("flex size-8 shrink-0 items-center justify-center rounded-md border font-mono text-[0.65rem]", track.team_label === "home" ? "border-primary/20 bg-primary/5 text-primary" : track.team_label === "away" ? "border-status-info/25 bg-status-info/5 text-status-info" : "border-border-soft bg-secondary text-muted-foreground")}>T{index + 1}</span>
            <span className="min-w-0 flex-1"><span className="block truncate text-xs font-medium">{name ?? "Unassigned player"}</span><span className="mt-0.5 block text-[0.6rem] text-muted-foreground">{track.team_label === "home" ? "Toledo" : track.team_label === "away" ? "Opponent" : "Team unknown"}{track.track_confidence != null ? ` · ${Math.round(track.track_confidence * 100)}%` : ""}</span></span>
            <span className={cn("size-1.5 shrink-0 rounded-full", visible ? "bg-status-ok" : "bg-muted-foreground/25")} title={visible ? "Visible in this frame" : "Outside current frame"} />
          </button>;
        })}</div>
        {teamFilter !== "all" && !tracklets.some((track) => track.team_label === teamFilter) ? <p className="py-3 text-xs text-muted-foreground">No tracks assigned to this team yet.</p> : null}
      </> : <div className="rounded-lg border border-dashed border-border-soft px-3 py-4 text-center"><ScanLine className="mx-auto mb-2 size-6 text-muted-foreground/60" /><p className="text-xs font-medium">Tracking will appear here</p><p className="mt-1 text-[0.7rem] leading-relaxed text-muted-foreground">Process this film with a configured vision model to see player boxes and identities.</p></div>}
    </div>
  </Card>;
}

/**
 * Non-blocking, dismissable notice shown when field calibration failed for
 * this footage. Watching video and detection boxes stays fully functional —
 * suppression only affects spatial metrics, and the reason sentence comes
 * ready-to-read from the backend (never silent suppression).
 */
function CalibrationBanner({
  reason,
  onDismiss,
}: {
  reason: string | null;
  onDismiss: () => void;
}) {
  return (
    <div
      data-testid="calibration-banner"
      role="status"
      className="mt-3 flex items-start gap-2.5 rounded-lg border border-status-warn/45 bg-status-warn/10 px-3 py-2.5"
    >
      <span aria-hidden="true" className="font-bold text-status-warn">
        !
      </span>
      <div className="flex-1">
        <p className="m-0 text-[0.8rem]">
          {reason ??
            "The field couldn't be calibrated for this footage, so spatial metrics are hidden."}
        </p>
        <p className="mt-1 text-xs text-muted-foreground">
          Video and player boxes are unaffected — only spatial metrics are suppressed.
        </p>
      </div>
      <button
        type="button"
        data-testid="calibration-banner-dismiss"
        aria-label="Dismiss calibration notice"
        onClick={onDismiss}
        className="cursor-pointer border-none bg-transparent p-0.5 text-sm leading-none text-muted-foreground hover:text-foreground"
      >
        ✕
      </button>
    </div>
  );
}

function OverlayToggles({
  active,
  overlayState,
  onToggle,
  showTrails,
  onToggleTrails,
}: {
  active: ReadonlySet<OverlayLayerKey>;
  overlayState: OverlayState;
  onToggle: (key: OverlayLayerKey) => void;
  showTrails: boolean;
  onToggleTrails: () => void;
}) {
  const layersAvailable =
    overlayState.kind === "ready" || overlayState.kind === "empty"
      ? overlayState.payload.layers_available
      : null;
  return (
    <div
      data-testid="overlay-toggles"
      role="group"
      aria-label="Overlay layer toggles"
      className="mt-3 flex flex-wrap items-center gap-1.5 border-t border-border-soft pt-3"
    >
      <span className="mr-1 inline-flex items-center gap-1.5 text-[0.65rem] font-semibold uppercase tracking-wider text-muted-foreground"><Layers className="size-3.5" /> Layers</span>
      {LAYER_TOGGLES.map((layer) => {
        const isActive = active.has(layer.key);
        // ``raw`` and ``field`` are always available; data layers report
        // their availability so coaches know why a toggle is dimmed.
        const layerHasData =
          layer.key === "raw" || layer.key === "field"
            ? true
            : layersAvailable
              ? layersAvailable[layerKeyToAvailability(layer.key)]
              : true;
        return (
          <button
            key={layer.key}
            type="button"
            onClick={() => onToggle(layer.key)}
            aria-pressed={isActive}
            data-testid={`overlay-toggle-${layer.key}`}
            disabled={!layerHasData && layer.key !== "raw"}
            className={cn(
              "inline-flex min-h-8 items-center gap-1.5 rounded-md border px-2.5 py-1 text-[0.7rem] font-medium transition-colors",
              isActive && layerHasData
                ? "border-primary bg-primary/15 text-primary"
                : "border-border-soft bg-transparent text-muted-foreground hover:border-border hover:text-foreground",
              !layerHasData && "cursor-not-allowed opacity-55",
            )}
          >
            {isActive && layerHasData ? <Check className="size-3" aria-hidden /> : null}
            {layer.label}
            {!layerHasData && layer.key !== "raw" && layer.key !== "field"
              ? " · empty"
              : ""}
          </button>
        );
      })}
      <button type="button" aria-pressed={showTrails} disabled={!active.has("tracks") || active.has("raw") || !layersAvailable?.tracklets} onClick={onToggleTrails} className={cn("inline-flex min-h-8 items-center gap-1.5 rounded-md border px-2.5 py-1 text-[0.7rem] font-medium disabled:cursor-not-allowed disabled:opacity-45", showTrails && active.has("tracks") && layersAvailable?.tracklets ? "border-primary/35 bg-primary/10 text-primary" : "border-border-soft text-muted-foreground")}><Route className="size-3" /> Trails</button>
    </div>
  );
}

function layerKeyToAvailability(
  key: OverlayLayerKey,
): keyof ClipOverlayPayload["layers_available"] {
  switch (key) {
    case "tracks":
      return "tracklets";
    case "events":
      return "events";
    case "labels":
      return "labels";
    case "metrics":
      return "metrics";
    default:
      return "tracklets";
  }
}

/**
 * Seekable clip timeline: click or drag anywhere to scrub, click an event
 * marker to jump straight to it. The playhead tracks playback.
 */
function EventTimeline({
  events,
  clipDuration,
  currentTime,
  onSeek,
}: {
  events: ReadonlyArray<{ event: { id: string; event_type: string }; t: number }>;
  clipDuration: number;
  currentTime: number;
  onSeek: (t: number) => void;
}) {
  const trackRef = useRef<HTMLDivElement | null>(null);
  const draggingRef = useRef(false);

  const seekFromPointer = useCallback(
    (clientX: number) => {
      const track = trackRef.current;
      if (!track || clipDuration <= 0) return;
      const rect = track.getBoundingClientRect();
      const fraction = Math.min(1, Math.max(0, (clientX - rect.left) / rect.width));
      onSeek(fraction * clipDuration);
    },
    [clipDuration, onSeek],
  );

  if (clipDuration <= 0) return null;

  const playheadPct = Math.min(1, Math.max(0, currentTime / clipDuration));

  return (
    <div className="mt-4 rounded-lg border border-border-soft bg-background/30 px-3 pb-3 pt-2.5">
      <div className="mb-2 flex items-center justify-between gap-3"><h4 className="text-[0.65rem] font-semibold uppercase tracking-wider text-muted-foreground">Play timeline</h4><span className="font-mono text-[0.6rem] text-muted-foreground">{events.length} tagged event{events.length === 1 ? "" : "s"}</span></div>
      <div
        ref={trackRef}
        data-testid="event-timeline"
        role="slider"
        aria-label="Clip timeline scrubber"
        aria-valuemin={0}
        aria-valuemax={Math.round(clipDuration * 10) / 10}
        aria-valuenow={Math.round(currentTime * 10) / 10}
        aria-valuetext={`${currentTime.toFixed(1)} seconds`}
        aria-orientation="horizontal"
        tabIndex={0}
        className="relative h-9 cursor-pointer touch-none rounded-md border border-border-soft bg-secondary/60"
        onKeyDown={(e) => {
          if (e.target !== e.currentTarget) return;
          if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
            e.preventDefault();
            e.stopPropagation();
            onSeek(Math.min(clipDuration, Math.max(0, currentTime + (e.key === "ArrowRight" ? 1 : -1) * (e.shiftKey ? 1 : 0.1))));
          } else if (e.key === "Home" || e.key === "End") {
            e.preventDefault();
            e.stopPropagation();
            onSeek(e.key === "Home" ? 0 : clipDuration);
          }
        }}
        onPointerDown={(e) => {
          draggingRef.current = true;
          e.currentTarget.setPointerCapture(e.pointerId);
          seekFromPointer(e.clientX);
        }}
        onPointerMove={(e) => {
          if (draggingRef.current) seekFromPointer(e.clientX);
        }}
        onPointerUp={(e) => {
          draggingRef.current = false;
          e.currentTarget.releasePointerCapture(e.pointerId);
        }}
        onPointerCancel={() => {
          draggingRef.current = false;
        }}
      >
        <div aria-hidden className="pointer-events-none absolute inset-0 opacity-25" style={{ backgroundImage: "repeating-linear-gradient(to right, transparent 0, transparent calc(10% - 1px), var(--muted-foreground) calc(10% - 1px), var(--muted-foreground) 10%)", backgroundSize: "100% 7px", backgroundRepeat: "no-repeat", backgroundPosition: "center bottom" }} />
        {/* Elapsed fill */}
        <div
          aria-hidden
          className="absolute bottom-0 left-0 top-0 rounded-l bg-primary/15"
          style={{ width: `${playheadPct * 100}%` }}
        />
        {events.map(({ event, t }) => {
          const pct = Math.min(1, Math.max(0, t / clipDuration));
          return (
            <button
              key={event.id}
              type="button"
              data-testid={`event-marker-${event.id}`}
              title={`${event.event_type} · ${t.toFixed(1)}s`}
              aria-label={`Jump to ${event.event_type} at ${t.toFixed(1)} seconds`}
              onPointerDown={(e) => e.stopPropagation()}
              onClick={(e) => {
                e.stopPropagation();
                onSeek(t);
              }}
              className="absolute inset-y-0 z-10 w-5 -translate-x-1/2 cursor-pointer rounded-sm after:absolute after:inset-y-1.5 after:left-1/2 after:w-1 after:-translate-x-1/2 after:rounded-sm after:bg-primary hover:bg-primary/15"
              style={{ left: `${pct * 100}%` }}
            />
          );
        })}
        {/* Playhead */}
        <div
          aria-hidden
          data-testid="timeline-playhead"
          className="pointer-events-none absolute bottom-0 top-0 z-20 w-0.5 -translate-x-px rounded bg-foreground shadow-[0_0_8px_rgba(255,255,255,0.3)]"
          style={{ left: `${playheadPct * 100}%` }}
        />
      </div>
      <div aria-hidden className="mt-1.5 flex justify-between font-mono text-[0.6rem] text-muted-foreground"><span>0.0s</span><span>{(clipDuration / 2).toFixed(1)}s</span><span>{clipDuration.toFixed(1)}s</span></div>
      {events.length > 0 ? <div className="mt-3 flex flex-wrap gap-1.5">{events.map(({ event, t }) => <button key={event.id} type="button" onClick={() => onSeek(t)} aria-label={`Seek to ${event.event_type.replaceAll("_", " ")} at ${t.toFixed(1)} seconds`} className={cn("inline-flex min-h-7 items-center gap-2 rounded border px-2 text-[0.65rem] transition-colors", Math.abs(t - currentTime) <= 0.4 ? "border-primary/40 bg-primary/10 text-primary" : "border-border-soft text-muted-foreground hover:border-border hover:text-foreground")}><span className="size-1 rounded-full bg-current" /><span className="capitalize">{event.event_type.replaceAll("_", " ")}</span><span className="font-mono opacity-70">{t.toFixed(1)}s</span></button>)}</div> : null}
      {events.length === 0 && (
        <p data-testid="event-timeline-empty" className="mt-1 text-xs text-muted-foreground">
          No events tagged for this clip — drag the bar to scrub.
        </p>
      )}
    </div>
  );
}

function OverlaySummary({
  overlayState,
  showMetrics,
  showLabels,
}: {
  overlayState: OverlayState;
  showMetrics: boolean;
  showLabels: boolean;
}) {
  if (overlayState.kind === "loading") {
    return (
      <p data-testid="overlay-loading" className="mt-4 text-xs text-muted-foreground">
        Loading overlays…
      </p>
    );
  }
  if (overlayState.kind === "error") {
    return (
      <p data-testid="overlay-error" className="mt-4 text-xs text-status-danger">
        Could not load overlays: {overlayState.message}
      </p>
    );
  }
  if (overlayState.kind === "empty") {
    return (
      <p data-testid="overlay-empty" className="mt-4 text-xs text-muted-foreground">
        No overlays available for this clip yet.
      </p>
    );
  }

  const { payload } = overlayState;
  const missing = [
    payload.layers_available.tracklets ? null : "tracklets",
    payload.layers_available.events ? null : "events",
    payload.layers_available.labels ? null : "labels",
    payload.layers_available.metrics ? null : "metrics",
  ].filter((x): x is string => x != null);

  return (
    <div data-testid="overlay-summary" className="mt-4">
      <h3 className="font-display text-xs font-semibold uppercase tracking-widest text-muted-foreground">
        Overlays
      </h3>
      {missing.length > 0 && (
        <p data-testid="overlay-degraded" className="mt-1 text-xs text-muted-foreground">
          Missing layers: {missing.join(", ")}.
        </p>
      )}
      {payload.calibration && payload.calibration.analytics_safe === false && (
        <AnalyticsCard
          title="Spatial Metrics"
          state={{
            kind: "gated",
            reason: "Hidden for this footage — the field could not be calibrated reliably.",
          }}
          gatedReason={payload.calibration.reason ?? undefined}
          className="mt-2"
        />
      )}
      {showLabels && payload.labels.length > 0 && (
        <details data-testid="overlay-labels-list" className="mt-3 rounded-lg border border-border-soft p-3">
          <summary className="cursor-pointer text-xs font-medium text-muted-foreground">
            Model suggestions · {payload.labels.length}
          </summary>
          <ul className="mb-1 mt-3 list-disc pl-4.5 text-[0.8rem]">
            {payload.labels.slice(0, 6).map((lb) => (
              <li key={lb.id}>
                <strong>{lb.label_type}</strong>
                {": "}
                <span className="text-xs text-muted-foreground">
                  {summarizeLabelValue(lb.label_value)} · {lb.source}
                </span>
              </li>
            ))}
          </ul>
        </details>
      )}
      {showMetrics && payload.metrics.length > 0 && (
        <div data-testid="overlay-metrics-list" className="mt-2">
          <p className="text-[0.68rem] font-semibold uppercase tracking-widest text-muted-foreground">
            Metrics
          </p>
          <ul className="my-1 list-disc pl-4.5 text-[0.8rem]">
            {payload.metrics.slice(0, 8).map((m) => (
              <li key={m.id}>
                <strong>{m.metric_name}</strong>
                {": "}
                <span data-numeric className="font-mono text-xs text-muted-foreground">
                  {summarizeMetricValue(m.metric_value)}
                  {m.unit ? ` ${m.unit}` : ""}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

function summarizeLabelValue(value: Record<string, unknown>): string {
  if ("name" in value && typeof value.name === "string") return value.name;
  const entries = Object.entries(value).slice(0, 2);
  return entries.map(([k, v]) => `${k}=${String(v)}`).join(", ");
}

function summarizeMetricValue(value: Record<string, unknown>): string {
  if ("value" in value) return String(value.value);
  const entries = Object.entries(value).slice(0, 2);
  return entries.map(([k, v]) => `${k}=${String(v)}`).join(", ");
}


const EMPTY_TRACKLETS: OverlayTracklet[] = [];

/** Render-prop subscriber: re-renders only its children, at most every ``everyMs``. */
function Timed({
  clock,
  everyMs,
  children,
}: {
  clock: MediaClock;
  everyMs: number;
  children: (elementTime: number) => ReactNode;
}) {
  const time = useThrottledMediaTime(clock, everyMs);
  return <>{children(time)}</>;
}

/**
 * The per-frame layer: overlay boxes and the in-frame status. The only part
 * of the page that re-renders on every video frame.
 */
const FrameOverlay = memo(function FrameOverlay({
  clock,
  toLocal,
  trackTimeAt,
  overlayPayload,
  overlayLoading,
  fps,
  sourceStartTime,
  clipDuration,
  videoWidth,
  videoHeight,
  activeLayers,
  rawOnly,
  playerNamesById,
  metricTextByTrackletId,
  selectedTrackletId,
  onSelectTracklet,
  showTrails,
}: {
  clock: MediaClock;
  toLocal: (elementTime: number) => number;
  trackTimeAt: (localTime: number) => number;
  overlayPayload: ClipOverlayPayload | null;
  overlayLoading: boolean;
  fps: number;
  sourceStartTime: number;
  clipDuration: number;
  videoWidth: number | null;
  videoHeight: number | null;
  activeLayers: Set<OverlayLayerKey>;
  rawOnly: boolean;
  playerNamesById: ReadonlyMap<string, string>;
  metricTextByTrackletId: ReadonlyMap<string, string>;
  selectedTrackletId: string | null;
  onSelectTracklet: (id: string) => void;
  showTrails: boolean;
}) {
  const localTime = toLocal(useMediaTime(clock));
  const trackTime = trackTimeAt(localTime);
  const visibleCount = overlayPayload
    ? overlayPayload.tracklets.reduce((count, track) => {
        const index = activeTrackPointIndex(track, trackTime, fps);
        return index >= 0 && track.track_points[index]?.bbox != null ? count + 1 : count;
      }, 0)
    : 0;
  return (
    <>
      {overlayPayload && (
        <OverlayCanvas
          tracklets={overlayPayload.tracklets}
          events={overlayPayload.events}
          currentTimeSeconds={trackTime}
          eventCurrentTimeSeconds={localTime}
          sourceStartTimeSeconds={sourceStartTime}
          fps={fps}
          videoWidth={videoWidth}
          videoHeight={videoHeight}
          activeLayers={activeLayers}
          playerNamesById={playerNamesById}
          metricTextByTrackletId={metricTextByTrackletId}
          selectedTrackletId={selectedTrackletId}
          onSelectTracklet={onSelectTracklet}
          showTrails={showTrails}
        />
      )}
      <div className="pointer-events-none absolute inset-x-0 top-0 flex items-start justify-between gap-2 bg-gradient-to-b from-black/65 to-transparent px-4 pb-8 pt-3 text-[0.65rem] font-semibold uppercase tracking-wider text-white/85">
        <span className="inline-flex items-center gap-2 rounded-md border border-white/15 bg-black/35 px-2 py-1"><span className={cn("size-1.5 rounded-full", visibleCount && !rawOnly ? "bg-emerald-400" : "bg-white/45")} />{rawOnly ? "Original recording" : overlayLoading ? "Loading tracking" : visibleCount ? `${visibleCount} tracks in frame` : "No player boxes at this frame"}</span>
        <span className="rounded-md border border-white/15 bg-black/35 px-2 py-1 font-mono normal-case">{localTime.toFixed(2)} / {clipDuration.toFixed(2)}s</span>
      </div>
    </>
  );
});

/** True when the clip's stored media is the clean per-play playback proxy. */
function isCleanProxy(clip: ApiClip): boolean {
  const playback = (clip.provenance as { playback?: { kind?: unknown } } | null | undefined)?.playback;
  return playback?.kind === "clean_clip";
}

/** The storage object a ready review is playing. */
function playbackStorage(state: Extract<ReviewState, { kind: "ready" }>): { bucket: string; key: string } | null {
  const target = state.playbackUsesClipAsset ? state.clip.storage_uri : state.video.storage_uri ?? state.clip.storage_uri;
  return parseStorageUri(target ?? null);
}

/** Cheap change detector for the overlay poll (counts, not a deep compare). */
function overlaySignature(payload: ClipOverlayPayload): string {
  const points = payload.tracklets.reduce((total, track) => total + track.track_points.length, 0);
  return [
    payload.tracklets.length,
    points,
    payload.events.length,
    payload.labels.length,
    payload.metrics.length,
    payload.calibration?.analytics_safe ?? "",
    payload.tracklets.map((track) => `${track.player_id ?? ""}${track.team_label ?? ""}`).join(""),
  ].join("|");
}

/** Sibling clips per recording, briefly cached so stepping plays is instant. */
const siblingCache = new Map<string, { at: number; clips: ApiClip[] }>();
async function siblingClipsFor(videoId: string, token?: string): Promise<ApiClip[]> {
  const cached = siblingCache.get(videoId);
  if (cached && Date.now() - cached.at < 60_000) return cached.clips;
  const clips = [...(await fetchClipsForVideo(videoId, token))].sort((a, b) => a.start_time - b.start_time);
  siblingCache.set(videoId, { at: Date.now(), clips });
  return clips;
}

/** Start downloading the next play's proxy so prev/next feels instant. */
function usePreloadClip(clipId: string | null, token?: string) {
  useEffect(() => {
    if (!clipId) return;
    let cancelled = false;
    let element: HTMLVideoElement | null = null;
    const timer = window.setTimeout(async () => {
      try {
        const next = await fetchClip(clipId, token);
        const storage = parseStorageUri(next.storage_uri);
        if (cancelled || !storage || storage.bucket !== "clips" || !isCleanProxy(next)) return;
        const url = await fetchVideoDownloadUrl(storage.bucket, storage.key, token);
        if (cancelled || !url) return;
        element = document.createElement("video");
        element.preload = "auto";
        element.muted = true;
        element.src = url;
      } catch {
        // Preloading is an optimisation only.
      }
    }, 1500);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
      if (element) {
        element.removeAttribute("src");
        element.load();
      }
    };
  }, [clipId, token]);
}
