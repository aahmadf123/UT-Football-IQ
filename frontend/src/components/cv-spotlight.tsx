"use client";

/**
 * Tracking Spotlight — the vision pipeline's output, front and center on the
 * dashboard. Auto-loads the most recent processed video, lets the coach flip
 * through its detected plays, and renders playback with the model's tracking
 * overlays (bounding-box markers, path tails, event badges) exactly as Clip
 * Review draws them.
 *
 * Honesty rules (repo #96): overlays are only ever real model output. Mock
 * mode and offline mode show an explicit notice instead of fabricated boxes,
 * and a processed clip with no overlay layers says so.
 */

import Link from "next/link";
import { ArrowRight, ScanEye } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useAppState } from "@/lib/app-state";
import {
  fetchClipOverlays,
  fetchClipsForVideo,
  fetchVideoDownloadUrl,
  parseStorageUri,
} from "@/lib/api";
import type { ApiClip, ApiVideo, ClipOverlayPayload, OverlayLayerKey } from "@/lib/types";
import { OverlayCanvas } from "@/app/clip-review/overlay-canvas";
import { trackingTimeSeconds } from "@/lib/tracking-time";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { NativeSelect } from "@/components/ui/native-select";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState } from "@/components/composite/empty-state";
import { StatusBadge } from "@/components/composite/status-badge";
import { friendlyMessage } from "@/lib/api-error";

// Default playback frame rate when the parent video has no ``fps`` recorded
// (mirrors clip-review; the CV pipeline targets 30fps).
const DEFAULT_FPS = 30;

// The classic "CV is working" look: boxes + path tails + event badges.
const SPOTLIGHT_LAYERS: ReadonlySet<OverlayLayerKey> = new Set(["tracks", "events"]);

type ClipsState =
  | { kind: "loading" }
  | { kind: "empty" }
  | { kind: "error"; message: string }
  | { kind: "ready"; clips: ApiClip[] };

type PlaybackState =
  | { kind: "loading" }
  | { kind: "error"; message: string }
  | { kind: "ready"; payload: ClipOverlayPayload; playbackUrl: string | null; playbackUsesClipAsset: boolean; error: string | null };

export function CvSpotlight() {
  const { data, apiStatus, mockMode, authToken } = useAppState();

  // Latest fully-processed video — the only footage that can carry overlays.
  const latestReady = useMemo<ApiVideo | null>(() => {
    const ready = data.videos.filter((v) => v.status === "ready");
    if (ready.length === 0) return null;
    return [...ready].sort((a, b) =>
      (b.created_at ?? "").localeCompare(a.created_at ?? ""),
    )[0];
  }, [data.videos]);

  const [clipsState, setClipsState] = useState<ClipsState>({ kind: "loading" });
  const [selectedClipId, setSelectedClipId] = useState<string>("");
  const [playback, setPlayback] = useState<PlaybackState>({ kind: "loading" });
  const [fallbackClipId, setFallbackClipId] = useState<string | null>(null);
  const latestReadyId = latestReady?.id;
  const originalStorageUri = latestReady?.storage_uri;

  // Load the clip inventory for the spotlighted video.
  useEffect(() => {
    if (mockMode || !latestReadyId) return;
    let cancelled = false;
    setClipsState({ kind: "loading" });
    fetchClipsForVideo(latestReadyId, authToken)
      .then((clips) => {
        if (cancelled) return;
        if (clips.length === 0) {
          setClipsState({ kind: "empty" });
          return;
        }
        const sorted = [...clips].sort((a, b) => {
          const ap = a.play_number ?? Number.MAX_SAFE_INTEGER;
          const bp = b.play_number ?? Number.MAX_SAFE_INTEGER;
          if (ap !== bp) return ap - bp;
          return a.start_time - b.start_time;
        });
        setClipsState({ kind: "ready", clips: sorted });
        setSelectedClipId((cur) => (cur && sorted.some((c) => c.id === cur) ? cur : sorted[0].id));
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setClipsState({
          kind: "error",
          message: friendlyMessage(err),
        });
      });
    return () => {
      cancelled = true;
    };
  }, [mockMode, latestReadyId, authToken]);

  const selectedClip = useMemo(() => {
    if (clipsState.kind !== "ready") return null;
    return clipsState.clips.find((c) => c.id === selectedClipId) ?? clipsState.clips[0];
  }, [clipsState, selectedClipId]);
  const activeClipId = selectedClip?.id;
  const clipStorageUri = selectedClip?.storage_uri;

  // Load overlays + a signed playback URL for the selected play.
  useEffect(() => {
    if (!activeClipId || !latestReadyId) return;
    let cancelled = false;
    setPlayback({ kind: "loading" });
    (async () => {
      try {
        // Original film keeps the overlay switchable; some clip previews
        // already have permanent boxes burned into their pixels.
        const clipStorage = parseStorageUri(clipStorageUri);
        const cleanClipAvailable = clipStorage?.bucket === "clips";
        const target = fallbackClipId === activeClipId && cleanClipAvailable
          ? clipStorage
          : parseStorageUri(originalStorageUri) ?? clipStorage;
        const [payload, playbackUrl] = await Promise.all([
          fetchClipOverlays(activeClipId, authToken),
          target ? fetchVideoDownloadUrl(target.bucket, target.key, authToken).catch(() => null) : Promise.resolve(null),
        ]);
        if (cancelled) return;
        const playbackUsesClipAsset = Boolean(target && target === clipStorage);
        if (!playbackUrl && !playbackUsesClipAsset && cleanClipAvailable && fallbackClipId !== activeClipId) {
          setFallbackClipId(activeClipId);
          return;
        }
        setPlayback({ kind: "ready", payload, playbackUrl, playbackUsesClipAsset, error: null });
      } catch (err) {
        if (cancelled) return;
        setPlayback({
          kind: "error",
          message: friendlyMessage(err),
        });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [activeClipId, latestReadyId, clipStorageUri, originalStorageUri, fallbackClipId, authToken]);

  const onPlaybackError = () => {
    if (!selectedClip || playback.kind !== "ready") return;
    if (!playback.playbackUsesClipAsset && parseStorageUri(selectedClip.storage_uri)?.bucket === "clips" && fallbackClipId !== selectedClip.id) {
      setFallbackClipId(selectedClip.id);
    } else {
      setPlayback({ ...playback, error: "Playback failed. Open this play in Clip Review to refresh its link or create a compatible clip." });
    }
  };

  return (
    <section data-testid="cv-spotlight">
      <Card className="overflow-hidden gap-4 border-primary/15 py-5">
        <CardHeader className="flex flex-row flex-wrap items-center justify-between gap-3 px-5">
          <div>
            <p className="section-eyebrow mb-2">Computer vision <span className="ml-1.5 text-primary">/ Film playback</span></p>
            <h2 className="flex items-center gap-2 text-base font-semibold">
              <ScanEye aria-hidden className="size-[18px] text-primary" />
              Tracking Spotlight
            </h2>
            <p className="mt-1.5 max-w-2xl text-xs leading-relaxed text-muted-foreground">
              Watch your latest processed play with player boxes, movement trails, and detected events.
            </p>
          </div>
          {clipsState.kind === "ready" && selectedClip ? (
            <div className="flex flex-wrap items-center gap-2">
              <NativeSelect
                value={selectedClip.id}
                onChange={(e) => setSelectedClipId(e.target.value)}
                aria-label="Spotlighted play"
                data-testid="cv-spotlight-clip-picker"
                className="max-w-56"
              >
                {clipsState.clips.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.play_number != null ? `Play #${c.play_number}` : `Clip ${c.id.slice(0, 8)}`}
                    {` · ${(c.end_time - c.start_time).toFixed(1)}s`}
                  </option>
                ))}
              </NativeSelect>
              <Button asChild variant="outline" size="sm">
                <Link href={`/clip-review/?clipId=${encodeURIComponent(selectedClip.id)}`}>
                  Open in Clip Review <ArrowRight className="size-3.5" />
                </Link>
              </Button>
            </div>
          ) : null}
        </CardHeader>
        <CardContent className="px-5">
          <SpotlightBody
            mockMode={mockMode}
            apiStatus={apiStatus}
            latestReady={latestReady}
            clipsState={clipsState}
            selectedClip={selectedClip}
            playback={playback}
            onPlaybackError={onPlaybackError}
          />
        </CardContent>
      </Card>
    </section>
  );
}

function SpotlightBody({
  mockMode,
  apiStatus,
  latestReady,
  clipsState,
  selectedClip,
  playback,
  onPlaybackError,
}: {
  mockMode: boolean;
  apiStatus: ReturnType<typeof useAppState>["apiStatus"];
  latestReady: ApiVideo | null;
  clipsState: ClipsState;
  selectedClip: ApiClip | null;
  playback: PlaybackState;
  onPlaybackError: () => void;
}) {
  if (mockMode) {
    return (
      <EmptyState
        icon={ScanEye}
        title="Tracking preview needs real processed film"
        hint="Mock mode never draws fabricated boxes. Connect the backend and process film to watch live model output here."
        data-testid="cv-spotlight-mock"
      />
    );
  }
  if (apiStatus === "offline") {
    return (
      <EmptyState
        icon={ScanEye}
        title="Tracking preview unavailable — not connected to the team server"
        hint="Check your workspace connection to load the latest tracked play."
      />
    );
  }
  if (apiStatus === "loading" || apiStatus === "idle") {
    return (
      <div role="status" aria-busy="true" aria-label="Loading tracking spotlight">
        <Skeleton className="aspect-video w-full max-h-96" />
      </div>
    );
  }
  if (!latestReady) {
    return (
      <EmptyState
        icon={ScanEye}
        title="No processed film yet"
        hint="Add your recording, run a tracking profile, and watch real model output here. Bounding boxes follow the video as it plays."
        className="field-grid min-h-72 border-solid bg-background/70"
        action={<Button asChild variant="outline" size="sm"><Link href="/film-room/?tab=upload">Prepare your first recording <ArrowRight className="size-3.5" /></Link></Button>}
        data-testid="cv-spotlight-empty"
      />
    );
  }
  if (clipsState.kind === "loading" || (clipsState.kind === "ready" && playback.kind === "loading")) {
    return (
      <div role="status" aria-busy="true" aria-label="Loading tracking spotlight">
        <Skeleton className="aspect-video w-full max-h-96" />
      </div>
    );
  }
  if (clipsState.kind === "empty") {
    return (
      <EmptyState
        icon={ScanEye}
        title="No plays detected yet"
        hint={`${latestReady.filename} is processed, but play detection hasn't produced clips yet.`}
      />
    );
  }
  if (clipsState.kind === "error") {
    return (
      <EmptyState
        icon={ScanEye}
        title="Couldn't load the latest plays"
        hint={clipsState.message}
      />
    );
  }
  if (playback.kind === "error") {
    return (
      <EmptyState
        icon={ScanEye}
        title="Couldn't load tracking overlays"
        hint={playback.message}
      />
    );
  }
  if (!selectedClip || playback.kind !== "ready") return null;

  return (
    <SpotlightPlayer key={`${selectedClip.id}-${playback.playbackUrl}`} video={latestReady} clip={selectedClip} playback={playback} onPlaybackError={onPlaybackError} />
  );
}

function SpotlightPlayer({
  video,
  clip,
  playback,
  onPlaybackError,
}: {
  video: ApiVideo;
  clip: ApiClip;
  playback: Extract<PlaybackState, { kind: "ready" }>;
  onPlaybackError: () => void;
}) {
  const { payload, playbackUrl, playbackUsesClipAsset } = playback;
  const sourceFps = payload.source_fps ?? video.fps;
  const fps = sourceFps != null && sourceFps > 0 ? sourceFps : DEFAULT_FPS;
  const [intrinsicSize, setIntrinsicSize] = useState<{ width: number; height: number } | null>(null);

  // Rendered clip assets start at clip-local time 0; parent-video playback
  // must be shifted into the clip timeline (same rule as Clip Review).
  const [videoCurrentTime, setVideoCurrentTime] = useState(0);
  const clipLocalTime = playbackUsesClipAsset
    ? Math.max(0, videoCurrentTime)
    : Math.max(0, videoCurrentTime - clip.start_time);

  const videoRef = useRef<HTMLVideoElement | null>(null);
  const clipOffset = playbackUsesClipAsset ? 0 : clip.start_time;
  const clipDuration = Math.max(0, clip.end_time - clip.start_time);
  const onTimeUpdate = useCallback(() => {
    const el = videoRef.current;
    if (!el) return;
    // Reaching the recording's end pauses the element; resume after wrapping
    // so the spotlight keeps looping, while ordinary seeks keep a user pause.
    const ended = el.ended;
    if (clipDuration > 0 && (ended || el.currentTime < clipOffset || el.currentTime >= clipOffset + clipDuration - 0.03)) {
      el.currentTime = clipOffset;
      if (ended) void el.play()?.catch(() => undefined);
    }
    setVideoCurrentTime(el.currentTime);
  }, [clipOffset, clipDuration]);
  useEffect(() => {
    const el = videoRef.current;
    if (!el?.requestVideoFrameCallback) return;
    let handle = 0;
    const onFrame = () => {
      onTimeUpdate();
      handle = el.requestVideoFrameCallback(onFrame);
    };
    handle = el.requestVideoFrameCallback(onFrame);
    return () => el.cancelVideoFrameCallback(handle);
  }, [onTimeUpdate]);

  const trackletCount = payload.tracklets.length;
  const eventCount = payload.events.length;
  const hasOverlays = payload.layers_available.tracklets || payload.layers_available.events;

  return (
    <div data-testid="cv-spotlight-player">
      <div className="relative flex min-h-64 items-center justify-center overflow-hidden rounded-md border border-border-soft bg-black">
        {playbackUrl ? (
          <>
            <video
              ref={videoRef}
              src={playbackUrl}
              autoPlay
              muted
              loop={playbackUsesClipAsset}
              controls
              playsInline
              onTimeUpdate={onTimeUpdate}
              onSeeked={onTimeUpdate}
              onEnded={onTimeUpdate}
              onError={onPlaybackError}
              onLoadedMetadata={() => {
                const el = videoRef.current;
                if (!el) return;
                if (el.videoWidth > 0 && el.videoHeight > 0) setIntrinsicSize({ width: el.videoWidth, height: el.videoHeight });
                el.currentTime = clipOffset;
                setVideoCurrentTime(clipOffset);
              }}
              aria-label={`Tracked play ${clip.play_number ?? clip.id} video`}
              className="block max-h-[440px] w-full object-contain"
            />
            <OverlayCanvas
              tracklets={payload.tracklets}
              events={payload.events}
              currentTimeSeconds={trackingTimeSeconds(payload, clipLocalTime, clip.start_time)}
              eventCurrentTimeSeconds={clipLocalTime}
              sourceStartTimeSeconds={payload.source_start_time_seconds ?? clip.start_time}
              fps={fps}
              videoWidth={video.width ?? intrinsicSize?.width ?? null}
              videoHeight={video.height ?? intrinsicSize?.height ?? null}
              activeLayers={SPOTLIGHT_LAYERS}
            />
          </>
        ) : (
          <div className="p-6 text-center text-muted-foreground">
            <p className="m-0 text-sm font-semibold">Video not available</p>
            <p className="mt-2 text-xs">
              Overlay data loaded, but playback for this clip isn&rsquo;t reachable right now. Open
              it in Clip Review for details.
            </p>
          </div>
        )}
      </div>
      {playback.error ? <p role="alert" className="mt-2 rounded-md border border-status-danger/30 bg-status-danger/5 px-3 py-2 text-xs text-status-danger">{playback.error}</p> : null}
      <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
        {hasOverlays ? (
          <StatusBadge tone="ok" dot data-testid="cv-spotlight-live">
            Model tracking live
          </StatusBadge>
        ) : (
          <StatusBadge tone="neutral" dot>
            Overlays pending
          </StatusBadge>
        )}
        <span data-numeric className="font-mono">
          {trackletCount} track{trackletCount === 1 ? "" : "s"} · {eventCount} event
          {eventCount === 1 ? "" : "s"}
        </span>
        <span className="truncate">{video.filename}</span>
        {payload.calibration && payload.calibration.analytics_safe === false ? (
          <span className="text-status-warn">
            Spatial metrics suppressed for this footage — boxes and events are unaffected.
          </span>
        ) : null}
      </div>
    </div>
  );
}
