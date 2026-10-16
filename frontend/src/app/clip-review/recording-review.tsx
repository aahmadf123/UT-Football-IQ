"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ArrowLeft, ArrowUpRight, Film, ScanLine } from "lucide-react";
import { fetchClipOverlays, fetchClipsForVideo, fetchVideo, fetchVideoDownloadUrl, parseStorageUri } from "@/lib/api";
import { useAppState } from "@/lib/app-state";
import type { ApiClip, ApiVideo, ClipOverlayPayload, OverlayLayerKey } from "@/lib/types";
import { trackingTimeSeconds } from "@/lib/tracking-time";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { cn } from "@/lib/utils";
import { OverlayCanvas } from "./overlay-canvas";
import { VideoTransport } from "./video-transport";
import { friendlyMessage } from "@/lib/api-error";

const RECORDING_LAYERS = new Set<OverlayLayerKey>(["tracks", "labels", "events"]);

/** Original film stays playable before inference; completed tracking follows playback. */
export function RecordingReview({ videoId }: { videoId: string }) {
  const { authToken, data } = useAppState();
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const [recording, setRecording] = useState<ApiVideo | null>(null);
  const [clips, setClips] = useState<ApiClip[]>([]);
  const [overlay, setOverlay] = useState<{ clipId: string; payload: ClipOverlayPayload } | null>(null);
  const [url, setUrl] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [analysisError, setAnalysisError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [revision, setRevision] = useState(0);
  const [currentTime, setCurrentTime] = useState(0);
  const [duration, setDuration] = useState(0);
  const [showTracking, setShowTracking] = useState(true);
  const [intrinsicSize, setIntrinsicSize] = useState<{ width: number; height: number } | null>(null);
  const reload = useCallback(() => setRevision((value) => value + 1), []);
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    void (async () => {
      try {
        const video = await fetchVideo(videoId, authToken);
        const storage = parseStorageUri(video.storage_uri);
        if (!storage) throw new Error("This recording has no saved video file.");
        const playback = await fetchVideoDownloadUrl(storage.bucket, storage.key, authToken);
        if (!playback) throw new Error("Could not open the saved recording. Check storage and try again.");
        if (!cancelled) { setRecording(video); setUrl(playback); setDuration(video.duration_seconds ?? 0); }
      } catch (cause) { if (!cancelled) setError(friendlyMessage(cause)); }
      finally { if (!cancelled) setLoading(false); }
    })();
    return () => { cancelled = true; };
  }, [videoId, authToken, revision]);

  useEffect(() => {
    let cancelled = false;
    const load = () => fetchClipsForVideo(videoId, authToken).then((items) => {
      if (!cancelled) { setClips([...items].sort((a, b) => a.start_time - b.start_time)); setAnalysisError(null); }
    }).catch(() => { if (!cancelled) setAnalysisError("Play analysis is unavailable. You can still watch the original recording."); });
    void load();
    const interval = window.setInterval(() => { if (document.visibilityState !== "hidden") void load(); }, 5000);
    return () => { cancelled = true; window.clearInterval(interval); };
  }, [videoId, authToken, revision]);

  const activeClip = clips.find((clip) => currentTime >= clip.start_time && currentTime < clip.end_time);
  const activeClipId = activeClip?.id;
  useEffect(() => {
    if (!activeClipId) return;
    let cancelled = false;
    const load = () => fetchClipOverlays(activeClipId, authToken).then((payload) => {
      if (!cancelled) { setOverlay({ clipId: activeClipId, payload }); setAnalysisError(null); }
    }).catch(() => { if (!cancelled) setAnalysisError("Tracking could not be loaded for this play. Open play review to retry."); });
    void load();
    const interval = window.setInterval(() => { if (document.visibilityState !== "hidden") void load(); }, 5000);
    return () => { cancelled = true; window.clearInterval(interval); };
  }, [activeClipId, authToken, revision]);

  useEffect(() => {
    const element = videoRef.current;
    if (!element?.requestVideoFrameCallback) return;
    let handle = 0;
    const onFrame = (_now: number, metadata: VideoFrameCallbackMetadata) => {
      setCurrentTime(metadata.mediaTime);
      handle = element.requestVideoFrameCallback(onFrame);
    };
    handle = element.requestVideoFrameCallback(onFrame);
    return () => element.cancelVideoFrameCallback(handle);
  }, [url, loading]);

  const playerNames = useMemo(() => new Map((data?.players ?? []).map((player) => [player.id, `#${player.jersey} ${player.name}`])), [data?.players]);
  const payload = overlay && overlay.clipId === activeClipId ? overlay.payload : null;
  const fps = payload?.source_fps && payload.source_fps > 0 ? payload.source_fps : recording?.fps && recording.fps > 0 ? recording.fps : 30;
  const clipLocalTime = Math.max(0, currentTime - (activeClip?.start_time ?? 0));
  const seekTo = (seconds: number) => { if (videoRef.current) videoRef.current.currentTime = seconds; };

  return <div className="flex flex-col gap-5">
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div><p className="mb-2 flex items-center gap-2 text-[0.65rem] font-semibold uppercase tracking-[0.18em] text-muted-foreground"><Film className="size-3.5 text-primary" /> Recording studio</p><h2 className="break-words text-xl font-semibold tracking-tight">{recording?.filename ?? "Watch recording"}</h2><p className="mt-1 text-sm text-muted-foreground">{recording?.camera_label ?? "Camera view"} · Original film with available play tracking.</p></div>
      <Button asChild variant="outline" size="sm"><Link href={recording?.practice_session_id ? `/film-room/?tab=browse&sessionId=${encodeURIComponent(recording.practice_session_id)}` : "/film-room/?tab=browse"}><ArrowLeft className="size-3.5" /> Back to session</Link></Button>
    </div>
    <div className="grid items-start gap-5 xl:grid-cols-[minmax(0,1fr)_300px]">
      <Card className="min-w-0 gap-0 overflow-hidden border-border-soft py-0 shadow-none">
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border-soft px-4 py-3"><span className="inline-flex items-center gap-2 text-xs font-semibold"><ScanLine className="size-4 text-primary" /> {activeClip ? `Play ${activeClip.play_number ?? clips.indexOf(activeClip) + 1}` : "Original recording"}</span><button type="button" aria-pressed={showTracking} onClick={() => setShowTracking((value) => !value)} className={cn("min-h-8 rounded-md border px-3 text-xs font-medium", showTracking ? "border-primary/35 bg-primary/10 text-primary" : "border-border-soft text-muted-foreground")}>Player tracking {showTracking ? "on" : "off"}</button></div>
        {loading ? <div role="status" className="flex aspect-video items-center justify-center bg-black text-sm text-muted-foreground">Opening saved recording…</div> : null}
        {url && !loading ? <>
          <div className="relative aspect-video overflow-hidden bg-black [&:fullscreen]:h-screen [&:fullscreen]:w-screen" style={{ aspectRatio: intrinsicSize ? `${intrinsicSize.width} / ${intrinsicSize.height}` : undefined }}>
            <video ref={videoRef} key={url} src={url} playsInline className="absolute inset-0 h-full w-full cursor-pointer object-contain" aria-label={recording?.filename ?? "Recording"} onTimeUpdate={() => setCurrentTime(videoRef.current?.currentTime ?? 0)} onSeeked={() => setCurrentTime(videoRef.current?.currentTime ?? 0)} onClick={() => { const el = videoRef.current; if (el?.paused) void el.play().catch(() => undefined); else el?.pause(); }} onLoadedMetadata={() => { const el = videoRef.current; if (!el) return; if (Number.isFinite(el.duration)) setDuration(el.duration); if (el.videoWidth > 0 && el.videoHeight > 0) setIntrinsicSize({ width: el.videoWidth, height: el.videoHeight }); }} onError={() => setError("Playback failed. Refresh the link, or check that this recording uses a supported browser video format.")} />
            {payload && showTracking ? <OverlayCanvas tracklets={payload.tracklets} events={payload.events} currentTimeSeconds={trackingTimeSeconds(payload, clipLocalTime, activeClip?.start_time ?? 0)} eventCurrentTimeSeconds={clipLocalTime} sourceStartTimeSeconds={payload.source_start_time_seconds ?? activeClip?.start_time ?? 0} fps={fps} videoWidth={recording?.width ?? intrinsicSize?.width ?? null} videoHeight={recording?.height ?? intrinsicSize?.height ?? null} activeLayers={RECORDING_LAYERS} playerNamesById={playerNames} onSelectTracklet={activeClip ? () => { videoRef.current?.pause(); } : undefined} /> : null}
          </div>
          <div className="px-4 pb-4 pt-2"><VideoTransport key={url} videoRef={videoRef} fps={fps} clipOffset={0} duration={duration} currentLocalTime={currentTime} eventTimes={clips.map((clip) => clip.start_time)} />
            <label className="mt-4 block"><span className="mb-2 flex items-center justify-between text-[0.65rem] text-muted-foreground"><span className="font-semibold uppercase tracking-wider">Recording timeline</span><span className="font-mono">{currentTime.toFixed(1)} / {duration.toFixed(1)}s</span></span><input type="range" aria-label="Recording timeline" min={0} max={Math.max(duration, 0.01)} step={1 / fps} value={Math.min(currentTime, duration)} onChange={(event) => seekTo(Number(event.target.value))} className="block h-2 w-full cursor-pointer accent-[var(--primary)]" /></label>
            {analysisError ? <p role="status" className="mt-3 text-xs text-muted-foreground">{analysisError}</p> : null}
            {activeClip ? <div className="mt-4 flex items-center justify-between gap-3 rounded-lg border border-primary/20 bg-primary/5 px-3 py-2.5"><p className="text-xs text-muted-foreground">Inspect player identities, events, and corrections for this play.</p><Button asChild variant="outline" size="sm"><Link href={`/clip-review/?clipId=${encodeURIComponent(activeClip.id)}`}>Open play <ArrowUpRight className="size-3" /></Link></Button></div> : null}
          </div>
        </> : null}
        {error ? <div role="alert" className="p-4 text-sm text-status-danger">{error}<Button variant="outline" size="sm" className="ml-3" onClick={reload}>Refresh playback</Button></div> : null}
      </Card>
      <Card className="gap-0 border-border-soft py-0 shadow-none"><div className="flex items-center justify-between border-b border-border-soft p-4"><h3 className="text-sm font-semibold">Plays in this film</h3><span className="font-mono text-xs text-muted-foreground">{clips.length}</span></div><div className="max-h-[65vh] space-y-1 overflow-y-auto p-2">{clips.length ? clips.map((clip, index) => <div key={clip.id} className={cn("rounded-lg border p-3", activeClipId === clip.id ? "border-primary/35 bg-primary/5" : "border-transparent hover:bg-secondary/50")}><div className="flex items-center justify-between gap-2"><button type="button" onClick={() => seekTo(clip.start_time)} className="min-h-7 text-left text-xs font-semibold hover:text-primary">Play {clip.play_number ?? index + 1}</button><Link href={`/clip-review/?clipId=${encodeURIComponent(clip.id)}`} aria-label={`Review play ${clip.play_number ?? index + 1}`} className="rounded p-1 text-muted-foreground hover:bg-secondary hover:text-primary"><ArrowUpRight className="size-3.5" /></Link></div><p className="mt-1 font-mono text-[0.65rem] text-muted-foreground">{clip.start_time.toFixed(1)}–{clip.end_time.toFixed(1)}s · {(clip.end_time - clip.start_time).toFixed(1)}s</p></div>) : <div className="p-4"><p className="text-sm font-medium">Watch film right away</p><p className="mt-2 text-xs leading-relaxed text-muted-foreground">Processing is optional for playback. Detected plays and player tracking appear after analysis finishes.</p><Button asChild variant="outline" size="sm" className="mt-4"><Link href="/film-room/?tab=upload">Processing status</Link></Button></div>}</div></Card>
    </div>
    <Button asChild variant="outline" className="self-start"><Link href={`/film-room/?tab=review&videoId=${encodeURIComponent(videoId)}`}>Detected plays <ArrowUpRight className="size-3.5" /></Link></Button>
  </div>;
}
