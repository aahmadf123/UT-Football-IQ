"use client";

import Link from "next/link";
import { ArrowRight, Minus, Plus, Search, Film, X, Camera } from "lucide-react";
import { useCallback, useDeferredValue, useEffect, useMemo, useRef, useState } from "react";
import { useAppState } from "@/lib/app-state";
import { ConceptSearch } from "@/components/concept-search";
import {
  fetchClipsForVideo,
  fetchPracticeSessions,
  fetchVideos,
  type PracticeSessionFilters,
  type VideoFilters,
} from "@/lib/api";
import type { FetchState } from "@/lib/fetch-state";
import { POSSESSION_LABEL, SESSION_KIND_LABEL } from "@/lib/labels";
import type {
  ApiClip,
  ApiPracticeSessionGroup,
  ApiSession,
  ApiVideo,
  OurPossession,
} from "@/lib/types";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { NativeSelect } from "@/components/ui/native-select";
import { Skeleton } from "@/components/ui/skeleton";
import { FilterBar } from "@/components/composite/filter-bar";
import { StatusBadge, toneForVideoStatus } from "@/components/composite/status-badge";
import { useWorkspaceContext } from "@/lib/workspace-context";

interface LibraryData {
  sessions: ApiPracticeSessionGroup[];
  videos: ApiVideo[];
}

type LibraryState = FetchState<LibraryData>;

function formatDate(value: string | null | undefined): string {
  if (!value) return "Unknown date";
  try {
    return new Date(/^\d{4}-\d{2}-\d{2}$/.test(value) ? `${value}T12:00:00` : value).toLocaleDateString(undefined, {
      weekday: "short",
      month: "short",
      day: "numeric",
      year: "numeric",
    });
  } catch {
    return value;
  }
}

function sessionKey(s: ApiPracticeSessionGroup): string {
  return [
    s.practice_session_id ?? "",
    s.session_date ?? "",
    s.session_kind ?? "",
    s.opponent_team ?? "",
  ].join("|");
}

export function LibraryView({ savedSessions }: { savedSessions?: ReadonlyArray<Pick<ApiSession, "id" | "title">> }) {
  const { selectedDate, setSelectedDate, sessionType, setSessionType, mockMode, authToken, data } = useAppState();
  const workspace = useWorkspaceContext();
  const [opponent, setOpponent] = useState<string>("");
  const [possession, setPossession] = useState<"" | OurPossession>("");
  const [query, setQuery] = useState("");
  const searchQuery = useDeferredValue(query.trim().toLowerCase());
  const [status, setStatus] = useState("");
  const [sort, setSort] = useState<"newest" | "oldest">("newest");
  const [hasMore, setHasMore] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [pagingError, setPagingError] = useState<string | null>(null);
  const [state, setState] = useState<LibraryState>({ kind: "loading" });
  const requestVersion = useRef(0);
  const videoRevision = data.videos.map((video) => `${video.id}:${video.status}`).join(",");
  const sessionTitles = useMemo(() => new Map(savedSessions?.map((session) => [session.id, session.title]) ?? []), [savedSessions]);

  const buildFilters = useCallback(() => {
    const sessionFilters: PracticeSessionFilters = {};
    const videoFilters: VideoFilters = { limit: 200 };
    if (selectedDate) {
      sessionFilters.recorded_after = `${selectedDate}T00:00:00Z`;
      sessionFilters.recorded_before = `${selectedDate}T23:59:59.999999Z`;
      videoFilters.recorded_after = sessionFilters.recorded_after;
      videoFilters.recorded_before = sessionFilters.recorded_before;
    }
    if (sessionType !== "all") {
      sessionFilters.session_kind = sessionType;
      videoFilters.session_kind = sessionType;
    }
    if (opponent) {
      sessionFilters.opponent_team = opponent;
      videoFilters.opponent_team = opponent;
    }

    return { sessionFilters, videoFilters };
  }, [selectedDate, sessionType, opponent]);

  const load = useCallback(async () => {
    const version = ++requestVersion.current;
    const { sessionFilters, videoFilters } = buildFilters();
    setState({ kind: "loading" });
    setPagingError(null);
    setLoadingMore(false);
    try {
      const [sessions, videos] = await Promise.all([
        fetchPracticeSessions(sessionFilters, authToken),
        fetchVideos(videoFilters, authToken),
      ]);
      if (version !== requestVersion.current) return;
      setHasMore(videos.length === 200);
      if (sessions.length === 0 && videos.length === 0) {
        setState({ kind: "empty" });
      } else {
        setState({ kind: "ready", data: { sessions, videos } });
      }
    } catch (err) {
      if (version !== requestVersion.current) return;
      setState({
        kind: "error",
        message: err instanceof Error ? err.message : String(err),
      });
    }
  }, [buildFilters, authToken]);

  useEffect(() => {
    void load();
    return () => { requestVersion.current += 1; };
  }, [load, videoRevision]);

  async function loadMore() {
    if (state.kind !== "ready" || loadingMore) return;
    const version = requestVersion.current;
    setLoadingMore(true);
    setPagingError(null);
    try {
      const videos = await fetchVideos({ ...buildFilters().videoFilters, offset: state.data.videos.length }, authToken);
      if (version !== requestVersion.current) return;
      setHasMore(videos.length === 200);
      setState((current) => current.kind === "ready" ? { kind: "ready", data: { ...current.data, videos: [...new Map([...current.data.videos, ...videos].map((video) => [video.id, video])).values()] } } : current);
    } catch (cause) { if (version === requestVersion.current) setPagingError(cause instanceof Error ? cause.message : String(cause)); }
    finally { if (version === requestVersion.current) setLoadingMore(false); }
  }

  const filtered = Boolean(selectedDate || sessionType !== "all" || opponent || possession || query || status);
  function clearFilters() {
    setSelectedDate(""); setSessionType("all"); setOpponent(""); setPossession(""); setQuery(""); setStatus("");
  }

  // Group videos under their session group, filtering by possession on the
  // client (possession is a per-video field, not a session-level field).
  const grouped = useMemo(() => {
    if (state.kind !== "ready") return null;
    const videosForGroup = (group: ApiPracticeSessionGroup): ApiVideo[] => {
      const dateStr = group.session_date;
      return state.data.videos.filter((v) => {
        if (group.practice_session_id) {
          if (v.practice_session_id !== group.practice_session_id) return false;
        } else {
          if (v.practice_session_id) return false;
          const vDate = (v.recorded_at ?? v.created_at)?.slice(0, 10) ?? null;
          if (vDate !== dateStr) return false;
          if (group.session_kind && v.session_kind !== group.session_kind) return false;
          if (group.opponent_team && v.opponent_team !== group.opponent_team) return false;
        }
        return true;
      });
    };
    const sourceGroups = state.data.sessions.map((session) => ({ session, videos: videosForGroup(session) }));
    const groupedVideoIds = new Set(sourceGroups.flatMap((group) => group.videos.map((video) => video.id)));
    // Keep every returned recording visible, including older uploads without
    // a practice-session aggregate or a named session.
    const orphanGroups = new Map<string, { session: ApiPracticeSessionGroup; videos: ApiVideo[] }>();
    for (const video of state.data.videos) {
      if (groupedVideoIds.has(video.id)) continue;
      const session: ApiPracticeSessionGroup = { practice_session_id: video.practice_session_id, session_date: (video.recorded_at ?? video.created_at)?.slice(0, 10), session_kind: video.session_kind, opponent_team: video.opponent_team, video_count: 1 };
      const key = sessionKey(session);
      const group = orphanGroups.get(key);
      if (group) { group.videos.push(video); group.session.video_count += 1; } else orphanGroups.set(key, { session, videos: [video] });
    }
    const enriched = [...sourceGroups, ...orphanGroups.values()]
      .map(({ session, videos }) => {
        const title = session.practice_session_id ? sessionTitles.get(session.practice_session_id) ?? "" : "";
        const sessionMatches = `${title} ${session.session_date ?? ""} ${session.session_kind ?? ""} ${session.opponent_team ?? ""}`.toLowerCase().includes(searchQuery);
        return { session, videos: videos.filter((video) => (!possession || video.our_possession === possession) && (!status || video.status === status) && (!searchQuery || sessionMatches || `${video.filename} ${video.camera_label ?? ""} ${video.opponent_team ?? ""}`.toLowerCase().includes(searchQuery))) };
      })
      .filter((group) => group.videos.length > 0)
      .sort((a, b) => (sort === "newest" ? -1 : 1) * (a.session.session_date ?? "").localeCompare(b.session.session_date ?? ""));
    const practice = enriched.filter(
      (g) => (g.session.session_kind ?? "practice") !== "game",
    );
    const games = enriched.filter((g) => g.session.session_kind === "game");
    return { practice, games, count: enriched.reduce((count, group) => count + group.videos.length, 0) };
  }, [state, possession, status, searchQuery, sort, sessionTitles]);

  return (
    <div className="flex flex-col gap-4">
      <Card className="gap-4 py-5">
        <CardHeader>
          <h2 className="text-base font-semibold">
            Film Library
          </h2>
          <p className="mt-0.5 max-w-2xl text-xs text-muted-foreground">
            {workspace.description} Search recordings and filter your film by session, possession, or processing status.
          </p>
        </CardHeader>
        <CardContent className="flex flex-wrap items-end gap-3">
          <div className="w-full"><Label htmlFor="library-search" className="sr-only">Search film</Label><div className="relative"><Search className="pointer-events-none absolute left-3 top-3 size-4 text-muted-foreground" aria-hidden /><Input id="library-search" className="h-10 pl-10 pr-10" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search recording, session, camera, or opponent…" />{query ? <button type="button" aria-label="Clear film search" className="absolute right-2 top-2 rounded p-1 text-muted-foreground hover:text-foreground" onClick={() => setQuery("")}><X className="size-4" /></button> : null}</div></div>
          <FilterBar className="mb-0" />
          <div className="flex min-w-40 flex-col gap-1">
            <Label
              htmlFor="library-opponent"
              className="text-xs font-semibold text-muted-foreground"
            >
              Opponent
            </Label>
            <Input
              id="library-opponent"
              className="h-9"
              value={opponent}
              onChange={(e) => setOpponent(e.target.value)}
              placeholder="e.g. Northern Illinois"
              aria-label="Filter by opponent team"
            />
          </div>
          <div className="flex min-w-40 flex-col gap-1">
            <Label
              htmlFor="library-possession"
              className="text-xs font-semibold text-muted-foreground"
            >
              Possession
            </Label>
            <NativeSelect
              id="library-possession"
              value={possession}
              onChange={(e) => setPossession(e.target.value as "" | OurPossession)}
              aria-label="Filter by possession"
            >
              <option value="">All possessions</option>
              <option value="offense">Toledo Offense</option>
              <option value="defense">Toledo Defense</option>
              <option value="special_teams">Special Teams</option>
            </NativeSelect>
          </div>
          <div className="flex min-w-36 flex-col gap-1"><Label htmlFor="library-status" className="text-xs font-semibold text-muted-foreground">Status</Label><NativeSelect id="library-status" value={status} onChange={(event) => setStatus(event.target.value)}><option value="">All statuses</option><option value="uploaded">Uploaded</option><option value="queued">Queued</option><option value="processing">Processing</option><option value="ready">Ready</option><option value="partial">Partial</option><option value="failed">Failed</option></NativeSelect></div>
          <div className="flex min-w-36 flex-col gap-1"><Label htmlFor="library-sort" className="text-xs font-semibold text-muted-foreground">Sort by</Label><NativeSelect id="library-sort" value={sort} onChange={(event) => setSort(event.target.value as "newest" | "oldest")}><option value="newest">Newest first</option><option value="oldest">Oldest first</option></NativeSelect></div>
          {filtered ? <Button variant="ghost" size="sm" onClick={clearFilters}><X className="size-3.5" /> Clear filters</Button> : null}
        </CardContent>
      </Card>

      {state.kind === "loading" && (
        <div role="status" aria-busy="true" aria-label="Loading library" className="flex flex-col gap-2">
          <Skeleton className="h-16 w-full" />
          <Skeleton className="h-16 w-full" />
        </div>
      )}
      {state.kind === "offline" && (
        <Card>
          <CardContent>
            <h3 className="text-sm font-semibold">
              Library unavailable
            </h3>
            <p className="mt-1 text-xs text-muted-foreground">
              The Library requires a connection to the team server.
              {mockMode
                ? " Mock mode is active — server-backed library is hidden in the default UI."
                : " Film appears here once the system is back online."}
            </p>
          </CardContent>
        </Card>
      )}
      {state.kind === "error" && (
        <Alert variant="destructive" role="alert">
          <AlertTitle>Could not load library</AlertTitle>
          <AlertDescription>{state.message}</AlertDescription>
          <Button variant="outline" size="sm" className="mt-2 w-fit" onClick={load}>
            Retry
          </Button>
        </Alert>
      )}
      {state.kind === "empty" && (
        <Card>
          <CardContent>
            <h3 className="text-sm font-semibold">
              {filtered ? "No film matches these filters" : "No film yet"}
            </h3>
            <p className="mt-1 text-xs text-muted-foreground">
              {filtered ? "Try a different date, session type, or opponent to find your recordings." : "Add practice or game film to start your library. Organize recordings from every camera into a session."}
            </p>
            {filtered ? <Button variant="outline" size="sm" className="mt-4" onClick={clearFilters}>Clear filters</Button> : <Button asChild variant="outline" size="sm" className="mt-4"><Link href="/film-room/?tab=upload">Add your first recording <ArrowRight className="size-3.5" /></Link></Button>}
          </CardContent>
        </Card>
      )}
      {state.kind === "ready" && grouped && (
        <>
          <div className="flex flex-wrap items-center justify-between gap-2"><p className="section-eyebrow">Recordings <span className="ml-2 text-foreground">{grouped.count}</span></p><p className="text-[11px] text-muted-foreground">{state.data.videos.length} loaded{hasMore ? " · More recordings available" : ""}</p></div>
          {grouped.count === 0 ? <div className="rounded-lg border border-dashed border-border-soft p-8 text-center"><Search className="mx-auto mb-3 size-5 text-muted-foreground" aria-hidden /><h3 className="text-sm font-medium">No film matches these filters</h3><p className="mt-2 text-xs text-muted-foreground">Try a different search, date, possession, or processing status.</p><Button variant="outline" size="sm" className="mt-4" onClick={clearFilters}>Clear filters</Button></div> : null}
          {grouped.count > 0 ? <>
          <LibrarySection
            title="Practice & Scrimmage Sessions"
            kind="practice"
            groups={grouped.practice}
            sessionTitles={sessionTitles}
          />
          <LibrarySection title="Game Film" kind="game" groups={grouped.games} sessionTitles={sessionTitles} />
          </> : null}
          {pagingError ? <p role="alert" className="text-xs text-status-danger">Could not load more film: {pagingError}</p> : null}
          {hasMore ? <Button variant="outline" className="self-center" disabled={loadingMore} onClick={() => void loadMore()}>{loadingMore ? "Loading recordings…" : "Load more recordings"}</Button> : null}
        </>
      )}
      <details className="rounded-xl border border-border-soft bg-card p-4">
        <summary className="cursor-pointer text-sm font-medium">Search for a football concept</summary>
        <p className="mt-2 text-sm text-muted-foreground">Search uses the labels available in this library. Approximate matches need coach review.</p>
        <div className="mt-4"><ConceptSearch /></div>
      </details>
    </div>
  );
}

function LibrarySection({
  title,
  kind,
  groups,
  sessionTitles,
}: {
  title: string;
  kind: "practice" | "game";
  groups: Array<{ session: ApiPracticeSessionGroup; videos: ApiVideo[] }>;
  sessionTitles: ReadonlyMap<string, string>;
}) {
  return (
    <Card className="gap-4 py-5">
      <CardHeader className="flex flex-row items-center justify-between">
        <h2 className="text-sm font-semibold">{title}</h2><span className="text-[11px] text-muted-foreground">{groups.length} session{groups.length === 1 ? "" : "s"}</span>
      </CardHeader>
      <CardContent>
        {groups.length === 0 ? (
          <p className="text-xs text-muted-foreground">
            {kind === "game"
              ? "No game film matches the current filters."
              : "No practice or scrimmage sessions match the current filters."}
          </p>
        ) : (
          <div className="flex flex-col gap-3">
            {groups.map((g) => (
              <SessionCard key={sessionKey(g.session)} session={g.session} videos={g.videos} title={g.session.practice_session_id ? sessionTitles.get(g.session.practice_session_id) : undefined} />
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function SessionCard({
  session,
  videos,
  title,
}: {
  session: ApiPracticeSessionGroup;
  videos: ApiVideo[];
  title?: string;
}) {
  const [open, setOpen] = useState(false);
  const kindLabel = session.session_kind ? SESSION_KIND_LABEL[session.session_kind] : "Session";
  const opponentLabel =
    session.session_kind === "game" && session.opponent_team
      ? `vs. ${session.opponent_team}`
      : null;

  return (
    <div className="overflow-hidden rounded-lg border border-border-soft bg-background/25">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-label={`${open ? "Collapse" : "Expand"} session ${title ? `${title} · ` : ""}${formatDate(session.session_date)}`}
        className="flex min-h-11 w-full items-center justify-between gap-3 rounded-lg p-3 text-left transition-colors hover:bg-accent/40"
      >
        <span className="flex size-10 shrink-0 items-center justify-center rounded-md border border-border-soft bg-secondary/40"><Film className="size-4 text-primary/70" aria-hidden /></span>
        <div className="min-w-0 flex-1">
          <span className="text-[0.95rem] font-semibold">{title || formatDate(session.session_date)}</span>
          <div className="mt-0.5 text-xs text-muted-foreground">
            {title ? `${formatDate(session.session_date)} · ` : ""}
            {kindLabel}
            {opponentLabel ? ` · ${opponentLabel}` : ""}
            {" · "}
            {videos.length} recording{videos.length === 1 ? "" : "s"}{session.video_count > videos.length ? ` of ${session.video_count}` : ""}
          </div>
        </div>
        <span
          aria-hidden
          className="flex size-7 shrink-0 items-center justify-center rounded-md border border-border-soft text-muted-foreground"
        >
          {open ? <Minus className="size-4" /> : <Plus className="size-4" />}
        </span>
      </button>
      {open && (
        <div className="border-t border-border-soft p-3">
          {videos.length === 0 ? (
            <p className="text-xs text-muted-foreground">
              No videos match the current possession filter.
            </p>
          ) : (
            <div className="flex flex-col gap-2">
              {videos.map((v) => (
                <VideoRow key={v.id} video={v} />
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function VideoRow({ video }: { video: ApiVideo }) {
  const { authToken } = useAppState();
  const [expanded, setExpanded] = useState(false);
  const [clips, setClips] = useState<ApiClip[] | null>(null);
  const [clipsError, setClipsError] = useState<string | null>(null);
  const [loadingClips, setLoadingClips] = useState(false);

  useEffect(() => {
    if (!expanded || clips !== null) return;
    let cancelled = false;
    setLoadingClips(true);
    setClipsError(null);
    fetchClipsForVideo(video.id, authToken)
      .then((data) => {
        if (!cancelled) setClips(data);
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setClipsError(err instanceof Error ? err.message : String(err));
        }
      })
      .finally(() => {
        if (!cancelled) setLoadingClips(false);
      });
    return () => {
      cancelled = true;
    };
  }, [expanded, clips, video.id, authToken]);

  return (
    <div className="rounded-md border border-border-soft bg-card/60 p-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <span className="flex size-9 shrink-0 items-center justify-center rounded-md bg-secondary/50"><Camera className="size-4 text-muted-foreground" aria-hidden /></span>
        <div className="min-w-0 flex-1">
          <Link href={`/clip-review/?videoId=${encodeURIComponent(video.id)}`} className="block break-words text-sm font-medium hover:text-primary">{video.filename}</Link>
          <div className="mt-0.5 text-xs text-muted-foreground">
            {video.session_kind ? SESSION_KIND_LABEL[video.session_kind] : "Session"}
            {video.opponent_team ? ` · vs. ${video.opponent_team}` : ""}
            {video.our_possession ? ` · ${POSSESSION_LABEL[video.our_possession]}` : ""}
            {video.camera_label ? ` · ${video.camera_label}` : ""}
            {video.duration_seconds != null ? ` · ${Math.floor(video.duration_seconds / 60)}:${String(Math.floor(video.duration_seconds % 60)).padStart(2, "0")}` : ""}
          </div>
        </div>
        <StatusBadge tone={toneForVideoStatus(video.status)} dot className="capitalize">
          {video.status}
        </StatusBadge>
        <Button variant="outline" size="sm" aria-expanded={expanded} onClick={() => setExpanded((v) => !v)}>
          {expanded ? "Hide clips" : "Show clips"}
        </Button>
        <Button asChild variant="outline" size="sm"><Link href={`/clip-review/?videoId=${encodeURIComponent(video.id)}`}>Watch recording</Link></Button>
      </div>
      {expanded && (
        <div className="mt-2.5">
          {loadingClips && <p className="text-xs text-muted-foreground">Loading clips…</p>}
          {clipsError && <p className="text-xs text-status-danger">{clipsError}</p>}
          {clips && clips.length === 0 && (
            <p className="text-xs text-muted-foreground">No clips processed for this video yet.</p>
          )}
          {clips && clips.length > 0 && (
            <div className="flex flex-col gap-1.5">
              {clips.map((c) => (
                <ClipRow key={c.id} clip={c} />
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function ClipRow({ clip }: { clip: ApiClip }) {
  const possession = clip.our_possession ?? clip.side_of_ball ?? null;
  const possessionLabel = possession ? POSSESSION_LABEL[possession] : null;
  return (
    <Link
      href={`/clip-review/?clipId=${encodeURIComponent(clip.id)}`}
      className="group flex items-center justify-between gap-2 rounded-md border border-border-soft px-2.5 py-1.5 transition-colors hover:border-border hover:bg-accent/50"
    >
      <div>
        <span className="text-sm font-semibold">
          {clip.play_number != null ? `Play #${clip.play_number}` : `Clip ${clip.id.slice(0, 8)}`}
        </span>
        <div data-numeric className="mt-0.5 font-mono text-xs text-muted-foreground">
          {(clip.end_time - clip.start_time).toFixed(1)}s
          {possessionLabel ? ` · ${possessionLabel}` : ""}
          {clip.session_kind ? ` · ${SESSION_KIND_LABEL[clip.session_kind]}` : ""}
        </div>
      </div>
      <span className="inline-flex shrink-0 items-center gap-1 text-xs font-semibold text-primary opacity-80 transition-opacity group-hover:opacity-100">
        Review <ArrowRight className="size-3.5" />
      </span>
    </Link>
  );
}
