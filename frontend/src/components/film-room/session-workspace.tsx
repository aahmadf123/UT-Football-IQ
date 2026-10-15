"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { Plus, Upload, ArrowRight, Link2, RefreshCw } from "lucide-react";
import { useAppState } from "@/lib/app-state";
import { addSessionRecording, createSession, fetchClipsForVideo, fetchSession, fetchSessions, matchSessionPlays } from "@/lib/api";
import type { ApiClip, ApiSession, SessionKind } from "@/lib/types";
import { useUploadWidget } from "@/components/shared/upload-widget";
import { LibraryView } from "@/app/library/library-view";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { NativeSelect } from "@/components/ui/native-select";
import { StatusBadge, toneForVideoStatus } from "@/components/composite/status-badge";
import { SessionTransfer } from "@/components/film-room/session-transfer";

export function SessionWorkspace({ initialSessionId = "", onSelect }: { initialSessionId?: string; onSelect: (id: string) => void }) {
  const { authToken, data, refreshData, currentRole } = useAppState();
  const [sessions, setSessions] = useState<ApiSession[]>([]);
  const [selectedId, setSelectedId] = useState(initialSessionId);
  const [session, setSession] = useState<ApiSession | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [busy, setBusy] = useState(false);
  const [title, setTitle] = useState("");
  const [kind, setKind] = useState<SessionKind>("practice");
  const [recordedDate, setRecordedDate] = useState("");
  const [opponent, setOpponent] = useState("");
  const [libraryRole, setLibraryRole] = useState<"toledo" | "opponent">("toledo");
  const [attachId, setAttachId] = useState("");
  const [camera, setCamera] = useState("");
  const canEdit = currentRole === "admin" || currentRole === "analyst" || currentRole === "coach";
  const { widget, openFilePicker } = useUploadWidget({
    metadata: session ? { practice_session_id: session.id, session_kind: session.session_kind, library_role: session.library_role, opponent_team: session.opponent_team, recorded_at: session.recorded_at } : undefined,
    sessionTitle: session?.title,
  });

  const loadSessions = useCallback(async () => {
    setLoading(true);
    try {
      setSessions(await fetchSessions(authToken));
      setError(null);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setLoading(false);
    }
  }, [authToken]);

  useEffect(() => { void loadSessions(); }, [loadSessions]);

  useEffect(() => {
    if (!selectedId) { setSession(null); return; }
    let cancelled = false;
    const load = async () => {
      try {
        const value = await fetchSession(selectedId, authToken);
        if (!cancelled) { setSession(value); setError(null); }
      } catch (cause) {
        if (!cancelled) { setSession(null); setError(cause instanceof Error ? cause.message : String(cause)); }
      }
    };
    void load();
    const interval = setInterval(load, 5000);
    return () => { cancelled = true; clearInterval(interval); };
  }, [selectedId, authToken]);

  function select(id: string) { setSelectedId(id); setSession(null); onSelect(id); }

  async function saveSession(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const created = await createSession({ title: title.trim(), session_kind: kind, library_role: libraryRole, ...(recordedDate ? { recorded_at: `${recordedDate}T12:00:00Z` } : {}), ...(opponent.trim() ? { opponent_team: opponent.trim() } : {}) }, authToken);
      await loadSessions();
      select(created.id);
      setSession(created);
      setCreating(false);
      setTitle("");
    } catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
    finally { setBusy(false); }
  }

  async function attachRecording(event: React.FormEvent) {
    event.preventDefault();
    if (!session || !attachId) return;
    setBusy(true);
    setError(null);
    try {
      setSession(await addSessionRecording(session.id, { video_id: attachId, ...(camera.trim() ? { camera_label: camera.trim() } : {}) }, authToken));
      setAttachId(""); setCamera("");
      await refreshData();
    } catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
    finally { setBusy(false); }
  }

  return (
    <div className="flex flex-col gap-5">
      {widget}
      <Card>
        <CardHeader className="flex flex-row flex-wrap items-center justify-between gap-3">
          <div><h2 className="text-base font-semibold">Session workspace</h2><p className="mt-1 text-sm text-muted-foreground">Keep practice or game recordings together, with a name for each camera angle.</p></div>
          <div className="flex flex-wrap gap-2"><Button variant="ghost" size="sm" onClick={() => void loadSessions()} disabled={loading}><RefreshCw className="size-4" /> Refresh</Button>{canEdit ? <><SessionTransfer sessionId={selectedId || undefined} onImported={async (id) => { await loadSessions(); select(id); }} /><Button variant="outline" size="sm" onClick={() => setCreating((value) => !value)}><Plus className="size-4" /> New session</Button></> : null}</div>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <div className="max-w-xl"><Label htmlFor="film-session">Session</Label><NativeSelect id="film-session" className="mt-2" value={selectedId} onChange={(event) => select(event.target.value)} disabled={loading}>
            <option value="">All recordings</option>{sessions.map((item) => <option key={item.id} value={item.id}>{item.title}{item.recorded_at ? ` · ${item.recorded_at.slice(0, 10)}` : ""}</option>)}
          </NativeSelect></div>
          {loading ? <p role="status" className="text-sm text-muted-foreground">Loading sessions…</p> : null}
          {error ? <p role="alert" className="text-sm text-status-danger">{error}</p> : null}
          {creating ? <form onSubmit={saveSession} className="grid gap-4 rounded-lg border border-border-soft p-4 sm:grid-cols-2">
            <div><Label htmlFor="new-session-title">Session name</Label><Input id="new-session-title" required maxLength={160} value={title} onChange={(event) => setTitle(event.target.value)} placeholder="Tuesday practice · team period" className="mt-2" /></div>
            <div><Label htmlFor="new-session-date">Recorded date</Label><Input id="new-session-date" type="date" value={recordedDate} onChange={(event) => setRecordedDate(event.target.value)} className="mt-2" /></div>
            <div><Label htmlFor="new-session-kind">Session type</Label><NativeSelect id="new-session-kind" value={kind} onChange={(event) => setKind(event.target.value as SessionKind)} className="mt-2"><option value="practice">Practice</option><option value="scrimmage">Scrimmage</option><option value="game">Game</option></NativeSelect></div>
            <div><Label htmlFor="new-session-role">Film belongs to</Label><NativeSelect id="new-session-role" value={libraryRole} onChange={(event) => setLibraryRole(event.target.value as "toledo" | "opponent")} className="mt-2"><option value="toledo">Toledo</option><option value="opponent">Opponent scouting</option></NativeSelect></div>
            <div><Label htmlFor="new-session-opponent">Opponent, if applicable</Label><Input id="new-session-opponent" value={opponent} onChange={(event) => setOpponent(event.target.value)} className="mt-2" /></div>
            <div className="flex items-end gap-2"><Button disabled={busy || !title.trim()}>{busy ? "Creating…" : "Create session"}</Button><Button type="button" variant="ghost" onClick={() => setCreating(false)}>Cancel</Button></div>
          </form> : null}
        </CardContent>
      </Card>

      {!selectedId ? <LibraryView savedSessions={sessions} /> : session ? <>
        <Card>
          <CardHeader className="flex flex-row flex-wrap items-start justify-between gap-3">
            <div><h2 className="text-lg font-semibold">{session.title}</h2><p className="mt-1 text-sm text-muted-foreground">{session.session_kind ?? "Session"} · {session.library_role === "opponent" ? "Opponent film" : "Toledo film"}{session.opponent_team ? ` · vs. ${session.opponent_team}` : ""} · {session.recordings.length} recording{session.recordings.length === 1 ? "" : "s"}</p></div>
            {canEdit ? <Button onClick={openFilePicker}><Upload className="size-4" /> Add recordings</Button> : null}
          </CardHeader>
          <CardContent className="flex flex-col gap-3">
            {session.recordings.length === 0 ? <p className="text-sm text-muted-foreground">Add a recording from any camera to start this session.</p> : session.recordings.map((recording) => <div key={recording.id} className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-border-soft p-4">
              <div className="min-w-0"><p className="break-words font-medium">{recording.filename}</p><p className="mt-1 text-sm text-muted-foreground">{recording.camera_label || "Camera angle not named"}{recording.recorded_at ? ` · ${recording.recorded_at.slice(0, 10)}` : ""}</p></div>
              <div className="flex flex-wrap items-center gap-3"><StatusBadge tone={toneForVideoStatus(recording.status)} dot>{recording.status}</StatusBadge><Button asChild variant="outline" size="sm"><Link href={`/clip-review/?videoId=${recording.id}&sessionId=${session.id}`}>Watch recording</Link></Button><Button asChild variant="outline" size="sm"><Link href={`/film-room/?tab=review&sessionId=${session.id}&videoId=${recording.id}`}>Review plays <ArrowRight className="size-4" /></Link></Button></div>
            </div>)}
            {canEdit ? <details className="rounded-lg border border-border-soft p-4"><summary className="cursor-pointer text-sm font-medium">Group a recording already in your library</summary><form onSubmit={attachRecording} className="mt-4 flex flex-wrap items-end gap-3">
              <div className="min-w-52 flex-1"><Label htmlFor="session-attach-recording">Recording</Label><NativeSelect id="session-attach-recording" value={attachId} onChange={(event) => setAttachId(event.target.value)} className="mt-2"><option value="">Choose a recording</option>{data.videos.filter((video) => !video.practice_session_id || video.practice_session_id === session.id).filter((video) => !session.recordings.some((recording) => recording.id === video.id)).map((video) => <option key={video.id} value={video.id}>{video.filename}</option>)}</NativeSelect></div>
              <div><Label htmlFor="session-camera-label">Camera name</Label><Input id="session-camera-label" value={camera} onChange={(event) => setCamera(event.target.value)} placeholder="Sideline, end zone, drone…" className="mt-2" /></div><Button disabled={!attachId || busy}>{busy ? "Grouping…" : "Add to session"}</Button>
            </form></details> : null}
          </CardContent>
        </Card>
        <SessionPlays session={session} onUpdate={setSession} canEdit={canEdit} />
      </> : <p role="status" className="text-sm text-muted-foreground">{error ? "Choose another session or retry the connection." : "Loading this session…"}</p>}
    </div>
  );
}

function SessionPlays({ session, onUpdate, canEdit }: { session: ApiSession; onUpdate: (session: ApiSession) => void; canEdit: boolean }) {
  const { authToken } = useAppState();
  const [clips, setClips] = useState<ApiClip[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [playTitle, setPlayTitle] = useState("");
  const [existingPlayId, setExistingPlayId] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const recordingIds = session.recordings.map((recording) => recording.id).join(",");

  useEffect(() => {
    let cancelled = false;
    setSelected([]);
    setExistingPlayId("");
    setClips([]);
    const load = () => Promise.all(recordingIds.split(",").filter(Boolean).map((id) => fetchClipsForVideo(id, authToken)))
      .then((items) => { if (!cancelled) { setClips(items.flat()); setError(null); } })
      .catch((cause) => { if (!cancelled) setError(cause instanceof Error ? cause.message : String(cause)); });
    void load();
    const interval = setInterval(load, 5000);
    return () => { cancelled = true; clearInterval(interval); };
  }, [recordingIds, session.id, authToken]);

  const selectedRecordings = new Set(clips.filter((clip) => selected.includes(clip.id)).map((clip) => clip.video_id));
  async function match(event: React.FormEvent) {
    event.preventDefault();
    if (selectedRecordings.size < 2 && !existingPlayId) return;
    setSaving(true); setError(null);
    try {
      onUpdate(await matchSessionPlays(session.id, { clip_ids: selected, ...(playTitle.trim() ? { title: playTitle.trim() } : {}), ...(existingPlayId ? { play_id: existingPlayId } : {}) }, authToken));
      setSelected([]); setPlayTitle("");
    } catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
    finally { setSaving(false); }
  }

  return <Card><CardHeader><h2 className="text-base font-semibold">Plays across camera angles</h2><p className="text-sm text-muted-foreground">Connect views of the same play after checking each recording. Matching is manual; recording clocks may differ.</p></CardHeader><CardContent className="flex flex-col gap-4">
    {session.plays.length ? session.plays.map((play, index) => <div key={play.id} className="rounded-lg border border-border-soft p-4"><h3 className="font-medium">{play.title || `Play ${play.sequence ?? index + 1}`}</h3><div className="mt-3 flex flex-wrap gap-2">{play.views.map((view) => <Button key={view.id} asChild variant="outline" size="sm"><Link href={`/clip-review/?clipId=${view.clip_id}`}>{view.camera_label || session.recordings.find((recording) => recording.id === view.video_id)?.camera_label || "View recording"}<ArrowRight className="size-3.5" /></Link></Button>)}</div></div>) : <p className="text-sm text-muted-foreground">No camera views have been matched yet. Each recording remains available to review on its own.</p>}
    {error ? <p role="alert" className="text-sm text-status-danger">{error}</p> : null}
    {canEdit && clips.length > 0 ? <details className="rounded-lg border border-border-soft p-4"><summary className="cursor-pointer text-sm font-medium">Match play views</summary><form onSubmit={match} className="mt-4 flex flex-col gap-4">
      <div className="grid gap-3 sm:grid-cols-2"><div><Label htmlFor="match-existing-play">Add views to</Label><NativeSelect id="match-existing-play" value={existingPlayId} onChange={(event) => setExistingPlayId(event.target.value)} className="mt-2"><option value="">A new matched play</option>{session.plays.map((play, index) => <option key={play.id} value={play.id}>{play.title || `Play ${play.sequence ?? index + 1}`}</option>)}</NativeSelect></div><div><Label htmlFor="matched-play-title">Play name, optional</Label><Input id="matched-play-title" value={playTitle} onChange={(event) => setPlayTitle(event.target.value)} placeholder="Team period · rep 12" className="mt-2" /></div></div>
      <div className="max-h-80 overflow-y-auto rounded-lg border border-border-soft">{clips.map((clip) => { const recording = session.recordings.find((item) => item.id === clip.video_id); return <label key={clip.id} className="flex cursor-pointer items-start gap-3 border-b border-border-soft p-3 last:border-0"><input type="checkbox" className="mt-1 size-4 accent-[var(--primary)]" checked={selected.includes(clip.id)} onChange={(event) => setSelected((current) => event.target.checked ? [...current, clip.id] : current.filter((id) => id !== clip.id))} /><span className="min-w-0 text-sm"><span className="block font-medium">{recording?.camera_label || recording?.filename || "Recording"} · Play {clip.play_number ?? "—"}</span><span className="text-muted-foreground">{clip.start_time.toFixed(1)}–{clip.end_time.toFixed(1)} seconds · </span><Link href={`/clip-review/?clipId=${clip.id}`} className="text-primary hover:underline">Review before matching</Link></span></label>; })}</div>
      <Button className="self-start" disabled={saving || selected.length === 0 || (!existingPlayId && selectedRecordings.size < 2)}><Link2 className="size-4" />{saving ? "Matching…" : "Match selected views"}</Button>
    </form></details> : null}
  </CardContent></Card>;
}
