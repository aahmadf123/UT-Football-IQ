"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { PlayCircle } from "lucide-react";
import { friendlyMessage } from "@/lib/api-error";
import {
  fetchGameSummary,
  fetchVideos,
  type ApiGameSummary,
  type ApiPlayGroup,
  type ApiSideSummary,
} from "@/lib/api";
import { useAppState } from "@/lib/app-state";
import type { ApiVideo } from "@/lib/types";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { NativeSelect } from "@/components/ui/native-select";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState } from "@/components/composite/empty-state";
import { cn } from "@/lib/utils";
import { StatTile } from "@/components/charts/stat-tile";
import {
  ByDown,
  ByQuarter,
  GameFlow,
  PlayByPlay,
  RunVsPass,
  YardsDistribution,
  playlistHref,
} from "@/components/team-stats/game-charts";

export { playlistHref } from "@/components/team-stats/game-charts";

function pct(value: number | null): string {
  return value == null ? "—" : `${Math.round(value * 100)}%`;
}

function num(value: number | null, digits = 1): string {
  return value == null ? "—" : value.toFixed(digits);
}

function gameLabel(video: ApiVideo): string {
  const date = video.recorded_at ? new Date(video.recorded_at).toLocaleDateString() : "";
  const opponent = video.opponent_team ? `vs ${video.opponent_team}` : video.filename;
  return [date, opponent].filter(Boolean).join(" · ");
}

function PlayGroupLinks({ groups }: { groups: ApiPlayGroup[] }) {
  return (
    <ul className="flex flex-wrap gap-2">
      {groups.map((group) => (
        <li key={group.key}>
          <Link
            href={playlistHref(group.clip_ids)}
            className="inline-flex min-h-9 items-center gap-1.5 rounded-md border border-border-soft bg-secondary/40 px-3 py-1.5 text-sm hover:border-primary hover:text-primary"
          >
            <PlayCircle className="size-4" aria-hidden />
            {group.label}
            <span data-numeric className="font-mono text-muted-foreground">
              {group.plays}
            </span>
          </Link>
        </li>
      ))}
    </ul>
  );
}

function SideBreakdown({ side, offense, defense }: { side: ApiSideSummary; offense: ApiSideSummary | null; defense: ApiSideSummary | null }) {
  const log = side.play_log ?? [];
  const thirdRate = side.third_down_attempts ? side.third_down_conversions / side.third_down_attempts : null;
  return (
    <div className="flex flex-col gap-4">
      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
        <StatTile label="Yards per play" value={num(side.yards_per_play)} hint={`${side.yards} yards on ${side.plays} plays`} />
        <StatTile label="Success rate" value={pct(side.success_rate)} hint="50% / 70% / 100% of the distance by down" />
        <StatTile label="EPA per play" value={side.epa_per_play == null ? "—" : `${side.epa_per_play > 0 ? "+" : ""}${side.epa_per_play.toFixed(2)}`} hint="Expected points added" />
        <StatTile label="3rd down" value={`${side.third_down_conversions}/${side.third_down_attempts}`} hint={thirdRate == null ? "No 3rd downs" : `${Math.round(thirdRate * 100)}% converted`} />
        <StatTile label="Explosive plays" value={String(side.explosive_plays)} hint="Run 10+ · pass 20+" />
        <StatTile label="Turnovers" value={String(side.turnovers)} hint={`${side.rush_plays} runs · ${side.pass_plays} passes`} />
      </div>
      {log.length ? <PlayByPlay log={log} /> : null}
      <div className="grid gap-4 lg:grid-cols-2">
        <GameFlow offense={offense?.play_log ?? []} defense={defense?.play_log ?? []} />
        {log.length ? <YardsDistribution log={log} /> : null}
        <ByDown side={side} />
        <RunVsPass side={side} />
        {log.length ? <ByQuarter log={log} /> : null}
      </div>
      <div>
        <h3 className="mb-2 font-display text-xs font-semibold uppercase tracking-widest text-muted-foreground">
          Watch the plays
        </h3>
        <PlayGroupLinks groups={side.groups} />
      </div>
    </div>
  );
}

/**
 * Toledo's offense and defense for one game, from processed film linked to
 * the official play-by-play. Every number links to the plays behind it.
 */
export function GameSummaryView() {
  const { authToken } = useAppState();
  const token = authToken ?? undefined;
  const [games, setGames] = useState<ApiVideo[] | null>(null);
  const [videoId, setVideoId] = useState("");
  const [summary, setSummary] = useState<ApiGameSummary | null>(null);
  const [side, setSide] = useState<"offense" | "defense">("offense");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      const videos: ApiVideo[] = [];
      const limit = 200;
      for (let offset = 0; ; offset += limit) {
        const page = await fetchVideos({ session_kind: "game", limit, offset }, token);
        if (cancelled) return;
        videos.push(...page);
        if (page.length < limit) break;
      }
      if (cancelled) return;
      const sorted = [...videos].sort((a, b) =>
        (b.recorded_at ?? b.created_at ?? "").localeCompare(a.recorded_at ?? a.created_at ?? ""),
      );
      setGames(sorted);
      // Home links straight to one game with ?videoId=.
      const wanted = new URLSearchParams(window.location.search).get("videoId");
      const linked = sorted.find((game) => game.id === wanted)?.id;
      setVideoId((current) => current || linked || sorted[0]?.id || "");
    })().catch((err) => {
      if (!cancelled) setError(friendlyMessage(err, "load your games"));
    });
    return () => {
      cancelled = true;
    };
  }, [token]);

  useEffect(() => {
    if (!videoId) return;
    let cancelled = false;
    setSummary(null);
    setError(null);
    fetchGameSummary(videoId, token)
      .then((result) => {
        if (!cancelled) setSummary(result);
      })
      .catch((err) => {
        if (!cancelled) setError(friendlyMessage(err, "load this game"));
      });
    return () => {
      cancelled = true;
    };
  }, [videoId, token]);

  const selected = useMemo(() => games?.find((g) => g.id === videoId), [games, videoId]);
  const shown = summary ? summary[side] : null;

  if (games !== null && games.length === 0) {
    return (
      <EmptyState
        title="No game film yet"
        hint="Upload a game in Film Room. Once it's processed, the breakdown appears here with links to every play."
      />
    );
  }

  return (
    <Card>
      <CardHeader className="flex flex-row flex-wrap items-center justify-between gap-2">
        <h2 className="font-display text-base font-semibold uppercase tracking-wide">
          Game breakdown{summary?.opponent ? ` · ${summary.opponent}` : ""}
        </h2>
        <div className="flex flex-wrap gap-2">
          {games ? (
            <NativeSelect
              aria-label="Game"
              value={videoId}
              onChange={(e) => setVideoId(e.target.value)}
              className="max-w-72"
            >
              {games.map((g) => (
                <option key={g.id} value={g.id}>
                  {gameLabel(g)}
                </option>
              ))}
            </NativeSelect>
          ) : null}
          <div
            role="group"
            aria-label="Side of the ball"
            className="flex gap-1 rounded-lg border border-border-soft bg-secondary/40 p-1"
          >
            {(["offense", "defense"] as const).map((key) => (
              <button
                key={key}
                type="button"
                aria-pressed={side === key}
                onClick={() => setSide(key)}
                className={cn(
                  "min-h-8 rounded-md px-3 text-sm font-semibold",
                  side === key
                    ? "bg-gradient-to-b from-primary to-gold-strong text-primary-foreground"
                    : "text-muted-foreground hover:bg-accent hover:text-foreground",
                )}
              >
                Toledo {key}
              </button>
            ))}
          </div>
        </div>
      </CardHeader>
      <CardContent>
        {error ? (
          <p role="alert" className="text-sm text-status-danger">
            {error}
          </p>
        ) : summary === null ? (
          <Skeleton className="h-64 w-full" />
        ) : summary.linked_plays === 0 ? (
          <p className="text-sm text-muted-foreground">
            {summary.film_plays
              ? `${summary.film_plays} plays found in ${selected ? gameLabel(selected) : "this film"}. Official results attach automatically once the game's play-by-play is matched; check back shortly.`
              : "This film hasn't been split into plays yet. The breakdown appears after processing."}
          </p>
        ) : shown === null ? (
          <p className="text-sm text-muted-foreground">No Toledo {side} plays matched in this film.</p>
        ) : (
          <>
            <p className="mb-4 text-xs text-muted-foreground">
              {summary.linked_plays} of {summary.film_plays} film plays matched to the official
              play-by-play.
            </p>
            <SideBreakdown side={shown} offense={summary.offense} defense={summary.defense} />
          </>
        )}
      </CardContent>
    </Card>
  );
}
