"use client";

import Link from "next/link";
import { ArrowRight, Minus, Plus } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
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

interface LibraryData {
  sessions: ApiPracticeSessionGroup[];
  videos: ApiVideo[];
}

type LibraryState = FetchState<LibraryData>;

function formatDate(value: string | null | undefined): string {
  if (!value) return "Unknown date";
  try {
    return new Date(value).toLocaleDateString(undefined, {
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

export function LibraryView() {
  const { selectedDate, sessionType, mockMode, authToken } = useAppState();
  const [opponent, setOpponent] = useState<string>("");
  const [possession, setPossession] = useState<"" | OurPossession>("");
  const [state, setState] = useState<LibraryState>({ kind: "loading" });

  const load = useCallback(async () => {
    const baseUrl = process.env.NEXT_PUBLIC_API_URL;
    if (!baseUrl) {
      setState({ kind: "offline" });
      return;
    }

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

    setState({ kind: "loading" });
    try {
      const [sessions, videos] = await Promise.all([
        fetchPracticeSessions(sessionFilters, authToken),
        fetchVideos(videoFilters, authToken),
      ]);
      if (sessions.length === 0 && videos.length === 0) {
        setState({ kind: "empty" });
      } else {
        setState({ kind: "ready", data: { sessions, videos } });
      }
    } catch (err) {
      setState({
        kind: "error",
        message: err instanceof Error ? err.message : String(err),
      });
    }
  }, [selectedDate, sessionType, opponent, authToken]);

  useEffect(() => {
    load();
  }, [load]);

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
          const vDate = v.recorded_at ? v.recorded_at.slice(0, 10) : null;
          if (vDate !== dateStr) return false;
          if (group.session_kind && v.session_kind !== group.session_kind) return false;
          if (group.opponent_team && v.opponent_team !== group.opponent_team) return false;
        }
        if (possession) {
          // Strict filter: when a possession is selected, exclude videos
          // whose possession is unknown or does not match the selection.
          if (v.our_possession !== possession) return false;
        }
        return true;
      });
    };
    const enriched = state.data.sessions
      .map((s) => ({ session: s, videos: videosForGroup(s) }))
      .filter((g) => g.videos.length > 0 || !possession);
    const practice = enriched.filter(
      (g) => (g.session.session_kind ?? "practice") !== "game",
    );
    const games = enriched.filter((g) => g.session.session_kind === "game");
    return { practice, games };
  }, [state, possession]);

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHeader>
          <h2 className="font-display text-base font-semibold uppercase tracking-wide">
            Hudl-style Library
          </h2>
          <p className="mt-0.5 max-w-2xl text-xs text-muted-foreground">
            Practice and game film grouped by date and session. Filter by date, session kind,
            opponent, and possession.
          </p>
        </CardHeader>
        <CardContent className="flex flex-wrap items-end gap-3">
          <FilterBar className="mb-0" />
          <div className="flex min-w-40 flex-col gap-1">
            <Label
              htmlFor="library-opponent"
              className="font-display text-[0.68rem] font-semibold uppercase tracking-widest text-muted-foreground"
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
              className="font-display text-[0.68rem] font-semibold uppercase tracking-widest text-muted-foreground"
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
        </CardContent>
      </Card>

      <ConceptSearch />

      {state.kind === "loading" && (
        <div role="status" aria-busy="true" aria-label="Loading library" className="flex flex-col gap-2">
          <Skeleton className="h-16 w-full" />
          <Skeleton className="h-16 w-full" />
        </div>
      )}
      {state.kind === "offline" && (
        <Card>
          <CardContent>
            <h3 className="font-display text-sm font-semibold uppercase tracking-wide">
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
            <h3 className="font-display text-sm font-semibold uppercase tracking-wide">
              No film yet
            </h3>
            <p className="mt-1 text-xs text-muted-foreground">
              Upload practice or game film from the Film Room → Upload / Process Film tab or your
              Drone integration. Sessions appear here once at least one video lands in storage.
            </p>
          </CardContent>
        </Card>
      )}
      {state.kind === "ready" && grouped && (
        <>
          <LibrarySection
            title="Practice & Scrimmage Sessions"
            kind="practice"
            groups={grouped.practice}
          />
          <LibrarySection title="Game Film" kind="game" groups={grouped.games} />
        </>
      )}
    </div>
  );
}

function LibrarySection({
  title,
  kind,
  groups,
}: {
  title: string;
  kind: "practice" | "game";
  groups: Array<{ session: ApiPracticeSessionGroup; videos: ApiVideo[] }>;
}) {
  return (
    <Card>
      <CardHeader>
        <h2 className="font-display text-base font-semibold uppercase tracking-wide">{title}</h2>
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
              <SessionCard key={sessionKey(g.session)} session={g.session} videos={g.videos} />
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
}: {
  session: ApiPracticeSessionGroup;
  videos: ApiVideo[];
}) {
  const [open, setOpen] = useState(false);
  const kindLabel = session.session_kind ? SESSION_KIND_LABEL[session.session_kind] : "Session";
  const opponentLabel =
    session.session_kind === "game" && session.opponent_team
      ? `vs. ${session.opponent_team}`
      : null;

  return (
    <div className="rounded-lg border border-border-soft bg-secondary/20">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-label={`${open ? "Collapse" : "Expand"} session ${formatDate(session.session_date)}`}
        className="flex min-h-11 w-full items-center justify-between gap-3 rounded-lg p-3 text-left transition-colors hover:bg-accent/40"
      >
        <div>
          <span className="text-[0.95rem] font-semibold">{formatDate(session.session_date)}</span>
          <div className="mt-0.5 text-xs text-muted-foreground">
            {kindLabel}
            {opponentLabel ? ` · ${opponentLabel}` : ""}
            {" · "}
            {session.video_count} video{session.video_count === 1 ? "" : "s"}
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
        <div className="px-3 pb-3">
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
    <div className="rounded-md border border-border-soft p-2.5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="min-w-0 flex-1">
          <span className="text-[0.85rem] font-semibold">{video.filename}</span>
          <div className="mt-0.5 text-xs text-muted-foreground">
            {video.session_kind ? SESSION_KIND_LABEL[video.session_kind] : "Session"}
            {video.opponent_team ? ` · vs. ${video.opponent_team}` : ""}
            {video.our_possession ? ` · ${POSSESSION_LABEL[video.our_possession]}` : ""}
          </div>
        </div>
        <StatusBadge tone={toneForVideoStatus(video.status)} dot className="capitalize">
          {video.status}
        </StatusBadge>
        <Button variant="outline" size="sm" onClick={() => setExpanded((v) => !v)}>
          {expanded ? "Hide clips" : "Show clips"}
        </Button>
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
        <span className="text-[0.82rem] font-semibold">
          {clip.play_number != null ? `Play #${clip.play_number}` : `Clip ${clip.id.slice(0, 8)}`}
        </span>
        <div data-numeric className="mt-0.5 font-mono text-xs text-muted-foreground">
          {(clip.end_time - clip.start_time).toFixed(1)}s
          {possessionLabel ? ` · ${possessionLabel}` : ""}
          {clip.session_kind ? ` · ${SESSION_KIND_LABEL[clip.session_kind]}` : ""}
        </div>
      </div>
      <span className="inline-flex shrink-0 items-center gap-1 text-xs font-semibold uppercase tracking-wide text-primary opacity-80 transition-opacity group-hover:opacity-100">
        Review <ArrowRight className="size-3.5" />
      </span>
    </Link>
  );
}
