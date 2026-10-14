"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { fetchVideo, fetchVideoDownloadUrl, parseStorageUri } from "@/lib/api";
import { useAppState } from "@/lib/app-state";
import type { ApiVideo } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";

/** Raw recording review works before any CV model or play segmentation. */
export function RecordingReview({ videoId }: { videoId: string }) {
  const { authToken } = useAppState();
  const [recording, setRecording] = useState<ApiVideo | null>(null);
  const [url, setUrl] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [revision, setRevision] = useState(0);
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
        if (!cancelled) { setRecording(video); setUrl(playback); }
      } catch (cause) { if (!cancelled) setError(cause instanceof Error ? cause.message : String(cause)); }
      finally { if (!cancelled) setLoading(false); }
    })();
    return () => { cancelled = true; };
  }, [videoId, authToken, revision]);
  return <Card>
    <CardHeader className="flex flex-row flex-wrap items-start justify-between gap-3">
      <div><h2 className="text-lg font-semibold">{recording?.filename ?? "Watch recording"}</h2><p className="mt-1 text-sm text-muted-foreground">Original film · {recording?.camera_label ?? "Camera"}. Processing is optional for playback.</p></div>
      <Button asChild variant="outline" size="sm"><Link href={recording?.practice_session_id ? `/film-room/?tab=browse&sessionId=${encodeURIComponent(recording.practice_session_id)}` : "/film-room/?tab=browse"}>Back to session</Link></Button>
    </CardHeader>
    <CardContent>
      {loading && <p role="status">Opening saved recording…</p>}
      {url && !loading && <video key={url} src={url} controls playsInline className="max-h-[70vh] w-full rounded-lg bg-black" aria-label={recording?.filename ?? "Recording"} onError={() => setError("Playback failed. Refresh the link, or check that this recording uses a supported browser video format.")} />}
      {error && <div role="alert" className="mt-3 text-sm text-status-danger">{error}<Button variant="outline" size="sm" className="ml-3" onClick={reload}>Refresh playback</Button></div>}
      <div className="mt-4 flex gap-2"><Button asChild variant="outline"><Link href={`/film-room/?tab=review&videoId=${encodeURIComponent(videoId)}`}>Detected plays</Link></Button><Button asChild><Link href="/film-room/?tab=upload">Processing status</Link></Button></div>
    </CardContent>
  </Card>;
}
