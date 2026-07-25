"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ArrowLeft, Clapperboard } from "lucide-react";
import { AnalyticsCard } from "@/components/analytics-card";
import { ClipStateBadges } from "@/components/clip-state-badge";
import { FootballShell } from "@/components/shell/app-shell";
import { useAppState } from "@/lib/app-state";
import {
  fetchClip,
  fetchClipOverlays,
  fetchVideo,
  fetchVideoDownloadUrl,
  parseStorageUri,
} from "@/lib/api";
import type {
  ApiClip,
  ApiVideo,
  ClipOverlayPayload,
  OverlayLayerKey,
} from "@/lib/types";
import { POSSESSION_LABEL, SESSION_KIND_LABEL } from "@/lib/labels";
import { CorrectionsPanel } from "./corrections-panel";
import { OverlayCanvas, eventTimeSeconds } from "./overlay-canvas";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState } from "@/components/composite/empty-state";
import { StatLine } from "@/components/composite/stat-chip";
import { cn } from "@/lib/utils";

const LAYER_TOGGLES: ReadonlyArray<{ key: OverlayLayerKey; label: string }> = [
  { key: "raw", label: "Raw" },
  { key: "tracks", label: "Tracks" },
  { key: "labels", label: "Labels" },
  { key: "events", label: "Events" },
  { key: "metrics", label: "Metrics" },
  { key: "wireframe", label: "Wireframe" },
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
  if (!clipId) {
    return (
      <EmptyState
        icon={Clapperboard}
        title="No clip selected"
        hint="Open a clip from Film Room → Browse Film to review it."
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
  return <ClipReviewView clipId={clipId} />;
}

function ClipReviewView({ clipId }: { clipId: string }) {
  const { authToken } = useAppState();
  const [state, setState] = useState<ReviewState>({ kind: "loading" });
  const [overlayState, setOverlayState] = useState<OverlayState>({ kind: "loading" });

  useEffect(() => {
    const baseUrl = process.env.NEXT_PUBLIC_API_URL;
    if (!baseUrl) {
      setState({ kind: "offline" });
      setOverlayState({ kind: "loading" });
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const clip = await fetchClip(clipId, authToken);
        const video = await fetchVideo(clip.video_id, authToken);
        let playbackUrl: string | null = null;
        let playbackUnavailable: "none" | "no_storage_uri" | "url_generation_failed" = "none";
        try {
          const clipStorage = parseStorageUri(clip.storage_uri);
          const videoStorage = parseStorageUri(video.storage_uri);
          const target = clipStorage ?? videoStorage;
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
        setState({ kind: "ready", clip, video, playbackUrl, playbackUnavailable });
      } catch (err) {
        if (cancelled) return;
        setState({
          kind: "error",
          message: err instanceof Error ? err.message : String(err),
        });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [clipId, authToken]);

  // Overlay fetch is independent of the clip fetch — a clip with no overlays
  // (e.g. ingest still running) should still render the player + metadata.
  useEffect(() => {
    const baseUrl = process.env.NEXT_PUBLIC_API_URL;
    if (!baseUrl) return;
    let cancelled = false;
    setOverlayState({ kind: "loading" });
    (async () => {
      try {
        const payload = await fetchClipOverlays(clipId, authToken);
        if (cancelled) return;
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
          message: err instanceof Error ? err.message : String(err),
        });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [clipId, authToken]);

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
        <Button asChild variant="outline" size="sm" className="mt-2 w-fit">
          <Link href="/film-room/?tab=browse">
            <ArrowLeft className="size-3.5" /> Back to Film Room
          </Link>
        </Button>
      </Alert>
    );
  }

  return <ClipReviewReady state={state} overlayState={overlayState} />;
}

function ClipReviewReady({
  state,
  overlayState,
}: {
  state: Extract<ReviewState, { kind: "ready" }>;
  overlayState: OverlayState;
}) {
  const { clip, video, playbackUrl, playbackUnavailable } = state;
  const possession = clip.our_possession ?? clip.side_of_ball ?? video.our_possession ?? null;
  const possessionLabel = possession ? POSSESSION_LABEL[possession] : null;
  const sessionKindLabel = clip.session_kind
    ? SESSION_KIND_LABEL[clip.session_kind]
    : video.session_kind
      ? SESSION_KIND_LABEL[video.session_kind]
      : "Session";

  const fps = video.fps ?? DEFAULT_FPS;
  const [activeLayers, setActiveLayers] = useState<Set<OverlayLayerKey>>(
    () => new Set<OverlayLayerKey>(["tracks", "events", "labels", "metrics"]),
  );
  const playbackUsesClipAsset = Boolean(clip.storage_uri);
  // Parent-video playback uses full-video time and must be shifted into the
  // clip timeline. Rendered clip playback already starts at clip-local time 0.
  const [videoCurrentTime, setVideoCurrentTime] = useState(0);
  const clipLocalTime = playbackUsesClipAsset
    ? Math.max(0, videoCurrentTime)
    : Math.max(0, videoCurrentTime - clip.start_time);

  const videoRef = useRef<HTMLVideoElement | null>(null);
  const onTimeUpdate = useCallback(() => {
    const el = videoRef.current;
    if (el) setVideoCurrentTime(el.currentTime);
  }, []);

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

  const overlayPayload = overlayState.kind === "ready" || overlayState.kind === "empty"
    ? overlayState.payload
    : null;

  const overlayLayersForCanvas = useMemo(() => {
    if (activeLayers.has("raw")) return new Set<OverlayLayerKey>();
    return activeLayers;
  }, [activeLayers]);

  const eventsForTimeline = useMemo(() => {
    if (!overlayPayload) return [];
    return overlayPayload.events
      .map((e) => ({ event: e, t: eventTimeSeconds(e, fps) }))
      .filter((row): row is { event: typeof row.event; t: number } => row.t != null);
  }, [overlayPayload, fps]);

  // Calibration banner (explained suppression). Dismissal is per-clip so
  // navigating to a different clip resurfaces the notice.
  const calibration = overlayPayload?.calibration ?? null;
  const [calibrationDismissedFor, setCalibrationDismissedFor] = useState<string | null>(null);
  const dismissCalibrationBanner = useCallback(
    () => setCalibrationDismissedFor(clip.id),
    [clip.id],
  );

  return (
    <div className="grid items-start gap-4 xl:grid-cols-3">
      <Card className="xl:col-span-2">
        <CardContent>
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <div className="flex flex-wrap items-center gap-2">
                <h2 className="font-display text-lg font-semibold uppercase tracking-wide">
                  {clip.play_number != null
                    ? `Play #${clip.play_number}`
                    : `Clip ${clip.id.slice(0, 8)}`}
                </h2>
                <ClipStateBadges
                  isPreliminary={clip.is_preliminary}
                  reviewState={clip.review_state}
                />
              </div>
              <p className="mt-0.5 text-xs text-muted-foreground">
                {sessionKindLabel}
                {video.opponent_team ? ` · vs. ${video.opponent_team}` : ""}
                {possessionLabel ? ` · ${possessionLabel}` : ""}
              </p>
            </div>
            <Button asChild variant="outline" size="sm">
              <Link href="/film-room/?tab=browse">
                <ArrowLeft className="size-3.5" /> Film Room
              </Link>
            </Button>
          </div>

          {calibration &&
            calibration.analytics_safe === false &&
            calibrationDismissedFor !== clip.id && (
              <CalibrationBanner
                reason={calibration.reason}
                onDismiss={dismissCalibrationBanner}
              />
            )}

          <div className="relative mt-3 flex min-h-80 items-center justify-center overflow-hidden rounded-lg bg-black">
            {playbackUrl ? (
              <>
                <video
                  ref={videoRef}
                  src={playbackUrl}
                  controls
                  playsInline
                  onTimeUpdate={onTimeUpdate}
                  onSeeked={onTimeUpdate}
                  aria-label={`Clip ${clip.play_number ?? clip.id} video`}
                  className="block max-h-120 w-full"
                />
                {overlayPayload && (
                  <OverlayCanvas
                    tracklets={overlayPayload.tracklets}
                    events={overlayPayload.events}
                    currentTimeSeconds={clipLocalTime}
                    fps={fps}
                    videoWidth={video.width ?? null}
                    videoHeight={video.height ?? null}
                    activeLayers={overlayLayersForCanvas}
                  />
                )}
              </>
            ) : (
              <div className="p-6 text-center text-muted-foreground">
                <p className="m-0 text-sm font-semibold">Video not available</p>
                <p className="mt-2 text-xs">
                  {playbackUnavailable === "no_storage_uri"
                    ? "No storage URI found. The video may not have been uploaded or rendered yet."
                    : "Video playback failed or is unavailable. The Worker may not be deployed, the signed URL may have been rejected, or the file may be missing from storage."}
                </p>
              </div>
            )}
          </div>

          <p data-numeric className="mt-2 font-mono text-xs text-muted-foreground">
            {clip.start_time.toFixed(1)}s – {clip.end_time.toFixed(1)}s{" "}
            ({(clip.end_time - clip.start_time).toFixed(1)}s duration)
          </p>

          <OverlayToggles
            active={activeLayers}
            overlayState={overlayState}
            onToggle={toggleLayer}
          />

          {overlayPayload && (
            <EventTimeline
              events={eventsForTimeline}
              clipDuration={clip.end_time - clip.start_time}
            />
          )}
        </CardContent>
      </Card>

      <Card>
        <CardContent>
          <h2 className="font-display text-base font-semibold uppercase tracking-wide">
            Clip Metadata
          </h2>
          <div className="mt-3 flex flex-col gap-1.5">
            <StatLine label="Video" value={video.filename} />
            <StatLine label="Video status" value={video.status} />
            <StatLine label="Session" value={sessionKindLabel} />
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

          <OverlaySummary
            overlayState={overlayState}
            showMetrics={activeLayers.has("metrics") && !activeLayers.has("raw")}
            showLabels={activeLayers.has("labels") && !activeLayers.has("raw")}
          />

          <CorrectionsPanel clipId={clip.id} tracklets={overlayPayload?.tracklets ?? []} />

          <h3 className="mt-4 font-display text-xs font-semibold uppercase tracking-widest text-muted-foreground">
            Storage
          </h3>
          <p className="mt-1 break-all font-mono text-[0.68rem] text-muted-foreground/80">
            {clip.storage_uri ?? "Clip not yet rendered to storage."}
          </p>
        </CardContent>
      </Card>
    </div>
  );
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
}: {
  active: ReadonlySet<OverlayLayerKey>;
  overlayState: OverlayState;
  onToggle: (key: OverlayLayerKey) => void;
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
      className="mt-3 flex flex-wrap gap-2"
    >
      {LAYER_TOGGLES.map((layer) => {
        const isActive = active.has(layer.key);
        // ``raw`` and ``wireframe`` are always available; data layers report
        // their availability so coaches know why a toggle is dimmed.
        const layerHasData =
          layer.key === "raw" || layer.key === "wireframe"
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
              "rounded-full border px-2.5 py-1 text-xs transition-colors",
              isActive
                ? "border-primary bg-primary/15 text-primary"
                : "border-border-soft bg-transparent text-muted-foreground hover:border-border hover:text-foreground",
              !layerHasData && "cursor-not-allowed opacity-55",
            )}
          >
            {layer.label}
            {!layerHasData && layer.key !== "raw" && layer.key !== "wireframe"
              ? " · empty"
              : ""}
          </button>
        );
      })}
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

function EventTimeline({
  events,
  clipDuration,
}: {
  events: ReadonlyArray<{ event: { id: string; event_type: string }; t: number }>;
  clipDuration: number;
}) {
  if (clipDuration <= 0) return null;
  if (events.length === 0) {
    return (
      <p data-testid="event-timeline-empty" className="mt-3 text-xs text-muted-foreground">
        No events tagged for this clip.
      </p>
    );
  }
  return (
    <div
      data-testid="event-timeline"
      className="relative mt-3 h-7 rounded bg-secondary/60"
      aria-label="Event timeline"
    >
      {events.map(({ event, t }) => {
        const pct = Math.min(1, Math.max(0, t / clipDuration));
        return (
          <div
            key={event.id}
            data-testid={`event-marker-${event.id}`}
            title={event.event_type}
            className="absolute bottom-1 top-1 w-1 -translate-x-0.5 rounded-sm bg-primary"
            style={{ left: `${pct * 100}%` }}
          />
        );
      })}
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
        <div data-testid="overlay-labels-list" className="mt-2">
          <p className="text-[0.68rem] font-semibold uppercase tracking-widest text-muted-foreground">
            Labels
          </p>
          <ul className="my-1 list-disc pl-4.5 text-[0.8rem]">
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
        </div>
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
