"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { Plus, Upload, ArrowRight, Link2, RefreshCw, Search, FolderOpen, Camera, ChevronLeft, CalendarDays } from "lucide-react";
import { useAppState } from "@/lib/app-state";
import { addSessionRecording, createSession, fetchClipsForVideo, fetchSession, fetchAllSessions, fetchAllVideos, matchSessionPlays } from "@/lib/api";
import type { ApiClip, ApiSession, ApiVideo, SessionKind } from "@/lib/types";
import { useUploadWidget } from "@/components/shared/upload-widget";
import { LibraryView } from "@/app/library/library-view";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { NativeSelect } from "@/components/ui/native-select";
import { StatusBadge, toneForVideoStatus } from "@/components/composite/status-badge";
import { SessionTransfer } from "@/components/film-room/session-transfer";
import { useWorkspaceContext } from "@/lib/workspace-context";
import { friendlyMessage } from "@/lib/api-error";

export function SessionWorkspace({ initialSessionId = "", onSelect }: { initialSessionId?: string; onSelect: (id: string) => void }) {
  const { authToken, refreshData, currentRole } = useAppState();
  const [sessions, setSessions] = useState<ApiSession[]>([]);
  const [selection, setSelection] = useState({ input: initialSessionId, id: initialSessionId });
  // A changed URL/prop wins immediately, before effects can fetch its rows.
  const selectedId = selection.input === initialSessionId ? selection.id : initialSessionId;
  const [loadedSession, setSession] = useState<ApiSession | null>(null);
  const session = loadedSession?.id === selectedId ? loadedSession : null;
  const selectedIdRef = useRef(selectedId);
  const catalogRequest = useRef(0);
  const sessionRequest = useRef(0);
  const actionRequest = useRef(0);
  const recordingRequest = useRef(0);
  selectedIdRef.current = selectedId;
  const [loading, setLoading] = useState(true);
  const [recordings, setRecordings] = useState<ApiVideo[]>([]);
  const [recordingsLoading, setRecordingsLoading] = useState(true);
  const [recordingsError, setRecordingsError] = useState<string | null>(null);
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
  const [sessionQuery, setSessionQuery] = useState("");
  const [showAllSessions, setShowAllSessions] = useState(false);
  const workspace = useWorkspaceContext();
  const matchedSessions = sessions.filter((item) => `${item.title} ${item.opponent_team ?? ""} ${item.session_kind ?? ""} ${item.recorded_at ?? ""}`.toLowerCase().includes(sessionQuery.trim().toLowerCase()));
  const visibleSessions = showAllSessions || sessionQuery.trim() ? matchedSessions : matchedSessions.slice(0, 6);
  const canEdit = currentRole === "admin" || currentRole === "analyst" || currentRole === "coach";
  const memberIds = new Set(session?.recordings.map((recording) => recording.id));
  const availableRecordings = recordings.filter((video) => !memberIds.has(video.id) && (!video.practice_session_id || video.practice_session_id === selectedId));
  const alreadyGrouped = recordings.filter((video) => memberIds.has(video.id)).length;
  const groupedElsewhere = recordings.length - alreadyGrouped - availableRecordings.length;
  const { widget, openFilePicker } = useUploadWidget({
    metadata: session ? { practice_session_id: session.id, session_kind: session.session_kind, library_role: session.library_role, opponent_team: session.opponent_team, recorded_at: session.recorded_at } : undefined,
    sessionTitle: session?.title,
  });

  useEffect(() => {
    setSelection((current) => current.input === initialSessionId ? current : { input: initialSessionId, id: initialSessionId });
  }, [initialSessionId]);

  const loadSessions = useCallback(async () => {
    const request = ++catalogRequest.current;
    setLoading(true);
    try {
      const rows = await fetchAllSessions(authToken);
      if (request === catalogRequest.current) { setSessions(rows); setError(null); }
    } catch (cause) {
      if (request === catalogRequest.current) setError(friendlyMessage(cause));
    } finally {
      if (request === catalogRequest.current) setLoading(false);
    }
  }, [authToken]);

  useEffect(() => { void loadSessions(); return () => { ++catalogRequest.current; }; }, [loadSessions]);

  const loadRecordings = useCallback(async () => {
    const request = ++recordingRequest.current;
    setRecordingsLoading(true);
    setRecordingsError(null);
    try {
      const rows = await fetchAllVideos({}, authToken);
      if (request === recordingRequest.current) setRecordings(rows);
    } catch (cause) {
      if (request === recordingRequest.current) setRecordingsError(friendlyMessage(cause));
    } finally {
      if (request === recordingRequest.current) setRecordingsLoading(false);
    }
  }, [authToken]);

  useEffect(() => {
    setRecordings([]);
    if (canEdit) void loadRecordings();
    return () => { ++recordingRequest.current; };
  }, [canEdit, loadRecordings]);

  useEffect(() => {
    ++sessionRequest.current;
    ++actionRequest.current;
    setSession(null); setError(null); setAttachId(""); setCamera(""); setBusy(false);
    if (!selectedId) return;
    let cancelled = false;
    const load = async () => {
      const request = ++sessionRequest.current;
      try {
        const value = await fetchSession(selectedId, authToken);
        if (!cancelled && request === sessionRequest.current) { setSession(value); setError(null); }
      } catch (cause) {
        if (!cancelled && request === sessionRequest.current) { setSession(null); setError(friendlyMessage(cause)); }
      }
    };
    void load();
    const interval = setInterval(load, 5000);
    return () => { cancelled = true; ++sessionRequest.current; ++actionRequest.current; clearInterval(interval); };
  }, [selectedId, authToken]);

  function select(id: string) { setSelection({ input: initialSessionId, id }); setSession(null); onSelect(id); }

  async function saveSession(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    const request = ++actionRequest.current;
    try {
      const created = await createSession({ title: title.trim(), session_kind: kind, library_role: libraryRole, ...(recordedDate ? { recorded_at: `${recordedDate}T12:00:00Z` } : {}), ...(kind === "game" && opponent.trim() ? { opponent_team: opponent.trim() } : {}) }, authToken);
      await loadSessions();
      if (request !== actionRequest.current) return;
      select(created.id);
      setSession(created);
      setCreating(false);
      setTitle("");
    } catch (cause) { if (request === actionRequest.current) setError(friendlyMessage(cause)); }
    finally { if (request === actionRequest.current) setBusy(false); }
  }

  async function attachRecording(event: React.FormEvent) {
    event.preventDefault();
    if (!session || !attachId) return;
    setBusy(true);
    setError(null);
    const request = ++actionRequest.current;
    const sessionId = session.id;
    try {
      const updated = await addSessionRecording(sessionId, { video_id: attachId, ...(camera.trim() ? { camera_label: camera.trim() } : {}) }, authToken);
      if (request !== actionRequest.current || selectedIdRef.current !== sessionId) return;
      ++sessionRequest.current;
      setSession(updated);
      setAttachId(""); setCamera("");
      void loadRecordings();
      await refreshData();
    } catch (cause) { if (request === actionRequest.current) setError(friendlyMessage(cause)); }
    finally { if (request === actionRequest.current) setBusy(false); }
  }

  return (
    <div className="flex flex-col gap-5">
      {widget}
      <Card className="gap-4 py-5">
        <CardHeader className="flex flex-row flex-wrap items-center justify-between gap-3">
          <div><p className="section-eyebrow mb-2">{workspace.libraryLabel}</p><h2 className="text-base font-semibold">Session workspace</h2><p className="mt-1 text-xs text-muted-foreground">Practice and game film, organized by session and camera angle.</p></div>
          <div className="flex flex-wrap gap-2"><Button variant="ghost" size="sm" onClick={() => void loadSessions()} disabled={loading}><RefreshCw className="size-4" /> Refresh</Button>{canEdit ? <><SessionTransfer sessionId={selectedId || undefined} onImported={async (id) => { await loadSessions(); select(id); }} /><Button variant="outline" size="sm" onClick={() => setCreating((value) => !value)}><Plus className="size-4" /> New session</Button></> : null}</div>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <div className="flex flex-wrap items-end gap-3"><div className="min-w-56 flex-1"><Label htmlFor="film-session" className="text-xs text-muted-foreground">Session</Label><NativeSelect id="film-session" className="mt-2" value={selectedId} onChange={(event) => select(event.target.value)} disabled={loading}>
            <option value="">All recordings</option>{sessions.map((item) => <option key={item.id} value={item.id}>{item.title}{item.recorded_at ? ` · ${item.recorded_at.slice(0, 10)}` : ""}</option>)}
          </NativeSelect></div>{!selectedId && sessions.length > 0 ? <div className="min-w-56 flex-1"><Label htmlFor="session-search" className="text-xs text-muted-foreground">Find a session</Label><div className="relative mt-2"><Search className="pointer-events-none absolute left-3 top-3 size-3.5 text-muted-foreground" aria-hidden /><Input id="session-search" className="pl-9" value={sessionQuery} onChange={(event) => setSessionQuery(event.target.value)} placeholder="Search name, opponent, or date…" /></div></div> : null}{selectedId ? <Button variant="ghost" size="sm" onClick={() => select("")}><ChevronLeft className="size-4" /> All sessions</Button> : null}</div>
          {loading ? <p role="status" className="text-sm text-muted-foreground">Loading sessions…</p> : null}
          {error ? <p role="alert" className="text-sm text-status-danger">{error}</p> : null}
          {creating ? <form onSubmit={saveSession} className="grid gap-4 rounded-lg border border-border-soft p-4 sm:grid-cols-2">
            <div><Label htmlFor="new-session-title">Session name</Label><Input id="new-session-title" required maxLength={160} value={title} onChange={(event) => setTitle(event.target.value)} placeholder="Tuesday practice · team period" className="mt-2" /></div>
            <div><Label htmlFor="new-session-date">Recorded date</Label><Input id="new-session-date" type="date" value={recordedDate} onChange={(event) => setRecordedDate(event.target.value)} className="mt-2" /></div>
            <div><Label htmlFor="new-session-kind">Session type</Label><NativeSelect id="new-session-kind" value={kind} onChange={(event) => { const value = event.target.value as SessionKind; setKind(value); if (value !== "game") setOpponent(""); }} className="mt-2"><option value="practice">Practice</option><option value="scrimmage">Scrimmage</option><option value="game">Game</option></NativeSelect></div>
            <div><Label htmlFor="new-session-role">Film belongs to</Label><NativeSelect id="new-session-role" value={libraryRole} onChange={(event) => setLibraryRole(event.target.value as "toledo" | "opponent")} className="mt-2"><option value="toledo">Toledo</option><option value="opponent">Opponent scouting</option></NativeSelect></div>
            {kind === "game" ? <div><Label htmlFor="new-session-opponent">Opponent, if applicable</Label><Input id="new-session-opponent" value={opponent} onChange={(event) => setOpponent(event.target.value)} className="mt-2" /></div> : null}
            <div className="flex items-end gap-2"><Button disabled={busy || !title.trim()}>{busy ? "Creating…" : "Create session"}</Button><Button type="button" variant="ghost" onClick={() => setCreating(false)}>Cancel</Button></div>
          </form> : null}
        </CardContent>
      </Card>

      {!selectedId && sessions.length > 0 ? <section aria-label="Saved sessions"><div className="mb-3 flex items-center justify-between"><h2 className="section-eyebrow">Saved sessions <span className="ml-2 text-foreground">{matchedSessions.length}</span></h2>{matchedSessions.length > 6 && !sessionQuery.trim() ? <Button size="sm" variant="ghost" onClick={() => setShowAllSessions((value) => !value)}>{showAllSessions ? "Show recent sessions" : "Show all sessions"}</Button> : null}</div>{visibleSessions.length ? <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">{visibleSessions.map((item) => <button key={item.id} type="button" onClick={() => select(item.id)} className="workspace-surface group flex flex-col rounded-lg border border-border-soft p-4 text-left transition-colors hover:border-primary/40"><div className="mb-4 flex w-full items-center justify-between gap-3"><FolderOpen className="size-5 text-primary/80" aria-hidden /><span className="text-[10px] font-medium uppercase tracking-wider text-muted-foreground">{item.session_kind ?? "Session"}</span></div><h3 className="text-sm font-semibold group-hover:text-primary">{item.title}</h3><p className="mt-1 text-xs text-muted-foreground">{item.opponent_team ? `vs. ${item.opponent_team}` : item.library_role === "opponent" ? "Opponent scouting" : "Toledo film"}</p><div className="mt-4 flex w-full flex-wrap items-center gap-x-4 gap-y-2 border-t border-border-soft pt-3 text-[11px] text-muted-foreground"><span className="flex items-center gap-1.5"><CalendarDays className="size-3" aria-hidden />{(item.recorded_at ?? item.created_at).slice(0, 10)}</span><span className="flex items-center gap-1.5"><Camera className="size-3" aria-hidden />{item.recordings.length} recording{item.recordings.length === 1 ? "" : "s"}</span><ArrowRight className="ml-auto size-3.5 text-primary/70" aria-hidden /></div></button>)}</div> : <div className="rounded-lg border border-dashed border-border-soft p-6 text-center"><p className="text-sm text-muted-foreground">No sessions match “{sessionQuery}”.</p><Button variant="link" size="sm" onClick={() => setSessionQuery("")}>Clear search</Button></div>}</section> : null}

      {!selectedId ? <details className="rounded-lg border border-border-soft p-4" open={sessions.length === 0 || undefined}><summary className="cursor-pointer text-sm font-semibold">All recordings</summary><div className="mt-4"><LibraryView savedSessions={sessions} /></div></details> : session ? <>
        <Card>
          <CardHeader className="flex flex-row flex-wrap items-start justify-between gap-3">
            <div><p className="section-eyebrow mb-2">Session recordings</p><h2 className="text-lg font-semibold">{session.title}</h2><p className="mt-1 text-sm text-muted-foreground">{session.session_kind ?? "Session"} · {session.library_role === "opponent" ? "Opponent film" : "Toledo film"}{session.opponent_team ? ` · vs. ${session.opponent_team}` : ""} · {session.recordings.length} recording{session.recordings.length === 1 ? "" : "s"}</p></div>
            {canEdit ? <Button onClick={openFilePicker}><Upload className="size-4" /> Add recordings</Button> : null}
          </CardHeader>
          <CardContent className="flex flex-col gap-3">
            {session.recordings.length === 0 ? <p className="text-sm text-muted-foreground">Add a recording from any camera to start this session.</p> : session.recordings.map((recording) => <div key={recording.id} className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-border-soft p-4">
              <div className="flex min-w-0 items-center gap-3"><span className="flex size-11 shrink-0 items-center justify-center rounded-md border border-border-soft bg-secondary/40"><Camera className="size-4 text-primary/70" aria-hidden /></span><div className="min-w-0"><p className="break-words text-sm font-medium">{recording.filename}</p><p className="mt-1 text-xs text-muted-foreground">{recording.camera_label || "Camera angle not named"}{recording.recorded_at ? ` · ${recording.recorded_at.slice(0, 10)}` : ""}</p></div></div>
              <div className="flex flex-wrap items-center gap-3"><StatusBadge tone={toneForVideoStatus(recording.status)} dot>{recording.status}</StatusBadge><Button asChild size="sm"><Link href={`/film-room/?tab=review&sessionId=${session.id}&videoId=${recording.id}`}>Review plays <ArrowRight className="size-4" /></Link></Button><Button asChild variant="ghost" size="sm"><Link href={`/clip-review/?videoId=${recording.id}&sessionId=${session.id}`}>Full recording</Link></Button></div>
            </div>)}
            {canEdit ? <details className="rounded-lg border border-border-soft p-4"><summary className="cursor-pointer text-sm font-medium">Group a recording already in your library</summary><form onSubmit={attachRecording} className="mt-4 flex flex-wrap items-end gap-3">
              <div className="min-w-52 flex-1"><Label htmlFor="session-attach-recording">Recording</Label><NativeSelect id="session-attach-recording" value={attachId} onChange={(event) => setAttachId(event.target.value)} disabled={recordingsLoading || !!recordingsError} className="mt-2"><option value="">Choose a recording</option>{availableRecordings.map((video) => <option key={video.id} value={video.id}>{video.filename}</option>)}</NativeSelect></div>
              <div><Label htmlFor="session-camera-label">Camera name</Label><Input id="session-camera-label" value={camera} onChange={(event) => setCamera(event.target.value)} placeholder="Sideline, end zone, drone…" className="mt-2" /></div><Button disabled={!availableRecordings.some((video) => video.id === attachId) || busy || recordingsLoading || !!recordingsError}>{busy ? "Grouping…" : "Add to session"}</Button>
            </form>
              <div className="mt-3 flex flex-col gap-2 text-xs text-muted-foreground" aria-live="polite">
                {recordingsLoading ? <p role="status">Loading uploaded recordings…</p> : recordingsError ? <p role="alert">Uploaded recordings could not be loaded. {recordingsError}</p> : <>
                  <p>{recordings.length} uploaded · {availableRecordings.length} available to add</p>
                  <p>Already in this session: {alreadyGrouped}. Grouped in other sessions: {groupedElsewhere}. Only ungrouped recordings or recordings already assigned to this session can be added.</p>
                  {availableRecordings.length === 0 ? <p>{recordings.length ? "All uploaded recordings are already grouped in this or another session." : "No uploaded recordings are available."} Use Add recordings to upload directly to this session.</p> : null}
                </>}
                <div className="flex flex-wrap items-center gap-3"><Button type="button" variant="ghost" size="sm" disabled={recordingsLoading} onClick={() => void loadRecordings()}>{recordingsError ? "Retry uploaded recordings" : "Refresh uploaded recordings"}</Button><Link className="underline underline-offset-2" href="/film-room/?tab=review">Review all uploaded recordings</Link></div>
              </div>
            </details> : null}
          </CardContent>
        </Card>
        <SessionPlays key={session.id} session={session} onUpdate={(updated) => { if (updated.id === selectedIdRef.current) { ++sessionRequest.current; setSession(updated); } }} canEdit={canEdit} />
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
  const [playQuery, setPlayQuery] = useState("");
  const [showAllPlays, setShowAllPlays] = useState(false);
  const recordingIds = session.recordings.map((recording) => recording.id).join(",");
  const independent = clips.filter((clip) => !session.plays.some((play) => play.views.some((view) => view.clip_id === clip.id)));
  const filtered = independent.filter((clip) => `${clip.play_number ?? ""} ${session.recordings.find((recording) => recording.id === clip.video_id)?.camera_label ?? ""}`.toLowerCase().includes(playQuery.toLowerCase()));

  useEffect(() => {
    let cancelled = false;
    let request = 0;
    setSelected([]);
    setExistingPlayId("");
    setClips([]);
    const load = () => {
      const current = ++request;
      return Promise.all(recordingIds.split(",").filter(Boolean).map((id) => fetchClipsForVideo(id, authToken)))
        .then((items) => { if (!cancelled && current === request) { setClips(items.flat()); setError(null); } })
        .catch((cause) => { if (!cancelled && current === request) setError(friendlyMessage(cause)); });
    };
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
    } catch (cause) { setError(friendlyMessage(cause)); }
    finally { setSaving(false); }
  }

  return <Card><CardHeader><h2 className="text-base font-semibold">Plays across camera angles</h2><p className="text-sm text-muted-foreground">Connect views of the same play after checking each recording. Matching is manual; recording clocks may differ.</p></CardHeader><CardContent className="flex flex-col gap-4">
    {independent.length ? <section aria-label="Playable session reps"><label className="mb-3 flex flex-col gap-1.5 text-xs text-muted-foreground">Find a rep<Input type="search" value={playQuery} onChange={(event) => setPlayQuery(event.target.value)} placeholder="Play number or camera" /></label><div className="grid gap-2 sm:grid-cols-2">{(showAllPlays ? filtered : filtered.slice(0, 12)).map((clip) => <Link key={clip.id} className="flex items-center justify-between gap-3 rounded border border-border-soft px-3 py-3 text-sm hover:border-primary/40" href={`/clip-review/?${new URLSearchParams({ clipId: clip.id, sessionId: session.id, from: `/film-room/?sessionId=${session.id}` })}`}><span>{clip.label_data?.canonical_count_eligible === false ? "Source window" : "Play"} {clip.play_number ?? "—"}<span className="ml-2 text-xs text-muted-foreground">{session.recordings.find((recording) => recording.id === clip.video_id)?.camera_label || "Camera view"} · {(clip.end_time - clip.start_time).toFixed(1)}s</span></span><ArrowRight className="size-4 shrink-0" /></Link>)}</div>{filtered.length > 12 ? <Button variant="ghost" size="sm" className="mt-2" onClick={() => setShowAllPlays((value) => !value)}>{showAllPlays ? "Show recent reps" : `Show all ${filtered.length} reps`}</Button> : null}{filtered.length === 0 ? <p className="text-sm text-muted-foreground">No reps match this search.</p> : null}</section> : null}
    {session.plays.length ? session.plays.map((play, index) => <div key={play.id} className="rounded-lg border border-border-soft p-4"><h3 className="font-medium">{play.title || `${play.count_eligible === false ? "Source window" : "Play"} ${play.sequence ?? index + 1}`}</h3><div className="mt-3 flex flex-wrap gap-2">{play.views.map((view) => <Button key={view.id} asChild variant="outline" size="sm"><Link href={`/clip-review/?clipId=${view.clip_id}`}>{view.camera_label || session.recordings.find((recording) => recording.id === view.video_id)?.camera_label || "View recording"}<ArrowRight className="size-3.5" /></Link></Button>)}</div></div>) : <p className="text-sm text-muted-foreground">No camera views have been matched yet. Each recording remains available to review on its own.</p>}
    {error ? <p role="alert" className="text-sm text-status-danger">{error}</p> : null}
    {canEdit && clips.length > 0 ? <details className="rounded-lg border border-border-soft p-4"><summary className="cursor-pointer text-sm font-medium">Match play views</summary><form onSubmit={match} className="mt-4 flex flex-col gap-4">
      <div className="grid gap-3 sm:grid-cols-2"><div><Label htmlFor="match-existing-play">Add views to</Label><NativeSelect id="match-existing-play" value={existingPlayId} onChange={(event) => setExistingPlayId(event.target.value)} className="mt-2"><option value="">A new matched play</option>{session.plays.map((play, index) => <option key={play.id} value={play.id}>{play.title || `${play.count_eligible === false ? "Source window" : "Play"} ${play.sequence ?? index + 1}`}</option>)}</NativeSelect></div><div><Label htmlFor="matched-play-title">Play name, optional</Label><Input id="matched-play-title" value={playTitle} onChange={(event) => setPlayTitle(event.target.value)} placeholder="Team period · rep 12" className="mt-2" /></div></div>
      <div className="max-h-80 overflow-y-auto rounded-lg border border-border-soft">{clips.map((clip) => { const recording = session.recordings.find((item) => item.id === clip.video_id); return <label key={clip.id} className="flex cursor-pointer items-start gap-3 border-b border-border-soft p-3 last:border-0"><input type="checkbox" className="mt-1 size-4 accent-[var(--primary)]" checked={selected.includes(clip.id)} onChange={(event) => setSelected((current) => event.target.checked ? [...current, clip.id] : current.filter((id) => id !== clip.id))} /><span className="min-w-0 text-sm"><span className="block font-medium">{recording?.camera_label || recording?.filename || "Recording"} · {clip.label_data?.canonical_count_eligible === false ? "Source window" : "Play"} {clip.play_number ?? "—"}</span><span className="text-muted-foreground">{clip.start_time.toFixed(1)}–{clip.end_time.toFixed(1)} seconds · </span><Link href={`/clip-review/?clipId=${clip.id}`} className="text-primary hover:underline">Review before matching</Link></span></label>; })}</div>
      <Button className="self-start" disabled={saving || selected.length === 0 || (!existingPlayId && selectedRecordings.size < 2)}><Link2 className="size-4" />{saving ? "Matching…" : "Match selected views"}</Button>
    </form></details> : null}
  </CardContent></Card>;
}
