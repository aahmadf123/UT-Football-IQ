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
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { NativeSelect } from "@/components/ui/native-select";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState } from "@/components/composite/empty-state";
import { StatusBadge } from "@/components/composite/status-badge";

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
  | { kind: "ready"; payload: ClipOverlayPayload; playbackUrl: string | null };

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

  // Load the clip inventory for the spotlighted video.
  useEffect(() => {
    if (mockMode || !process.env.NEXT_PUBLIC_API_URL || !latestReady) return;
    let cancelled = false;
    setClipsState({ kind: "loading" });
    fetchClipsForVideo(latestReady.id, authToken)
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
          message: err instanceof Error ? err.message : String(err),
        });
      });
    return () => {
      cancelled = true;
    };
  }, [mockMode, latestReady, authToken]);

  const selectedClip = useMemo(() => {
    if (clipsState.kind !== "ready") return null;
    return clipsState.clips.find((c) => c.id === selectedClipId) ?? clipsState.clips[0];
  }, [clipsState, selectedClipId]);

  // Load overlays + a signed playback URL for the selected play.
  useEffect(() => {
    if (!selectedClip || !latestReady) return;
    let cancelled = false;
    setPlayback({ kind: "loading" });
    (async () => {
      try {
        const payload = await fetchClipOverlays(selectedClip.id, authToken);
        let playbackUrl: string | null = null;
        try {
          const target =
            parseStorageUri(selectedClip.storage_uri) ?? parseStorageUri(latestReady.storage_uri);
          if (target) {
            playbackUrl = await fetchVideoDownloadUrl(target.bucket, target.key, authToken);
          }
        } catch {
          playbackUrl = null;
        }
        if (cancelled) return;
        setPlayback({ kind: "ready", payload, playbackUrl });
      } catch (err) {
        if (cancelled) return;
        setPlayback({
          kind: "error",
          message: err instanceof Error ? err.message : String(err),
        });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [selectedClip, latestReady, authToken]);

  return (
    <section data-testid="cv-spotlight">
      <Card>
        <CardHeader className="flex flex-row flex-wrap items-center justify-between gap-3">
          <div>
            <h2 className="flex items-center gap-2 font-display text-base font-semibold uppercase tracking-wide">
              <ScanEye aria-hidden className="size-[18px] text-primary" />
              Tracking Spotlight
            </h2>
            <p className="mt-0.5 max-w-2xl text-xs text-muted-foreground">
              The vision model&rsquo;s output on your latest processed play — player boxes, path
              tails, and detected events, straight from the pipeline.
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
        <CardContent>
          <SpotlightBody
            mockMode={mockMode}
            apiStatus={apiStatus}
            latestReady={latestReady}
            clipsState={clipsState}
            selectedClip={selectedClip}
            playback={playback}
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
}: {
  mockMode: boolean;
  apiStatus: ReturnType<typeof useAppState>["apiStatus"];
  latestReady: ApiVideo | null;
  clipsState: ClipsState;
  selectedClip: ApiClip | null;
  playback: PlaybackState;
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
  if (!process.env.NEXT_PUBLIC_API_URL || apiStatus === "offline") {
    return (
      <EmptyState
        icon={ScanEye}
        title="Tracking preview unavailable — not connected to the team server"
        hint="The latest tracked play appears here when the system is back online."
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
        hint="Upload film and run processing — the latest tracked play appears here automatically."
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
    <SpotlightPlayer video={latestReady} clip={selectedClip} playback={playback} />
  );
}

function SpotlightPlayer({
  video,
  clip,
  playback,
}: {
  video: ApiVideo;
  clip: ApiClip;
  playback: Extract<PlaybackState, { kind: "ready" }>;
}) {
  const { payload, playbackUrl } = playback;
  const fps = video.fps ?? DEFAULT_FPS;

  // Rendered clip assets start at clip-local time 0; parent-video playback
  // must be shifted into the clip timeline (same rule as Clip Review).
  const playbackUsesClipAsset = Boolean(clip.storage_uri);
  const [videoCurrentTime, setVideoCurrentTime] = useState(0);
  const clipLocalTime = playbackUsesClipAsset
    ? Math.max(0, videoCurrentTime)
    : Math.max(0, videoCurrentTime - clip.start_time);

  const videoRef = useRef<HTMLVideoElement | null>(null);
  const onTimeUpdate = useCallback(() => {
    const el = videoRef.current;
    if (el) setVideoCurrentTime(el.currentTime);
  }, []);

  const trackletCount = payload.tracklets.length;
  const eventCount = payload.events.length;
  const hasOverlays = payload.layers_available.tracklets || payload.layers_available.events;

  return (
    <div data-testid="cv-spotlight-player">
      <div className="relative flex min-h-64 items-center justify-center overflow-hidden rounded-lg bg-black">
        {playbackUrl ? (
          <>
            <video
              ref={videoRef}
              src={playbackUrl}
              autoPlay
              muted
              loop
              controls
              playsInline
              onTimeUpdate={onTimeUpdate}
              onSeeked={onTimeUpdate}
              aria-label={`Tracked play ${clip.play_number ?? clip.id} video`}
              className="block max-h-96 w-full"
            />
            <OverlayCanvas
              tracklets={payload.tracklets}
              events={payload.events}
              currentTimeSeconds={clipLocalTime}
              fps={fps}
              videoWidth={video.width ?? null}
              videoHeight={video.height ?? null}
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
