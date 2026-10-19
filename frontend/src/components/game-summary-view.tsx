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
import { StatChip } from "@/components/composite/stat-chip";
import { cn } from "@/lib/utils";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

/** Clip review URL that steps through exactly these plays. */
export function playlistHref(clipIds: string[]): string {
  const params = new URLSearchParams({ clipId: clipIds[0], playlist: clipIds.join(",") });
  return `/clip-review/?${params.toString()}`;
}

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

function SideBreakdown({ side }: { side: ApiSideSummary }) {
  return (
    <div className="flex flex-col gap-5">
      <div className="grid grid-cols-2 gap-2 md:grid-cols-4">
        <StatChip label="Plays" value={side.plays} />
        <StatChip label="Yards / play" value={num(side.yards_per_play)} hint={`${side.yards} YDS`} />
        <StatChip label="Success rate" value={pct(side.success_rate)} />
        <StatChip label="EPA / play" value={num(side.epa_per_play, 2)} />
        <StatChip
          label="3rd down"
          value={`${side.third_down_conversions}/${side.third_down_attempts}`}
          hint="CONVERTED"
        />
        <StatChip label="Explosive" value={side.explosive_plays} hint="RUN 10+ · PASS 20+" />
        <StatChip label="Turnovers" value={side.turnovers} />
        <StatChip
          label="Run / pass"
          value={`${side.rush_plays} / ${side.pass_plays}`}
          hint={side.plays ? `${Math.round((side.rush_plays / side.plays) * 100)}% RUN` : undefined}
        />
      </div>

      <Table>
        <TableHeader>
          <TableRow>
            <TableHead />
            <TableHead className="text-right">Plays</TableHead>
            <TableHead className="text-right">Yards / play</TableHead>
            <TableHead className="text-right">Success</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          <TableRow>
            <TableCell className="font-medium">Runs</TableCell>
            <TableCell data-numeric className="text-right font-mono">{side.rush_plays}</TableCell>
            <TableCell data-numeric className="text-right font-mono">
              {num(side.rush_yards_per_play)}
            </TableCell>
            <TableCell data-numeric className="text-right font-mono">
              {pct(side.rush_success_rate)}
            </TableCell>
          </TableRow>
          <TableRow>
            <TableCell className="font-medium">Passes</TableCell>
            <TableCell data-numeric className="text-right font-mono">{side.pass_plays}</TableCell>
            <TableCell data-numeric className="text-right font-mono">
              {num(side.pass_yards_per_play)}
            </TableCell>
            <TableCell data-numeric className="text-right font-mono">
              {pct(side.pass_success_rate)}
            </TableCell>
          </TableRow>
          {side.by_down.map((down) => (
            <TableRow key={down.down}>
              <TableCell className="font-medium">
                {["1st", "2nd", "3rd", "4th"][down.down - 1]} down
                <span className="ml-2 text-xs text-muted-foreground">
                  {pct(down.rush_share)} run
                </span>
              </TableCell>
              <TableCell data-numeric className="text-right font-mono">{down.plays}</TableCell>
              <TableCell data-numeric className="text-right font-mono">
                {num(down.yards_per_play)}
              </TableCell>
              <TableCell data-numeric className="text-right font-mono">
                {pct(down.success_rate)}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>

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
    fetchVideos({ session_kind: "game", limit: 200 }, token)
      .then((videos) => {
        if (cancelled) return;
        const sorted = [...videos].sort((a, b) =>
          (b.recorded_at ?? b.created_at ?? "").localeCompare(a.recorded_at ?? a.created_at ?? ""),
        );
        setGames(sorted);
        setVideoId((current) => current || sorted[0]?.id || "");
      })
      .catch((err) => {
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
            <SideBreakdown side={shown} />
          </>
        )}
      </CardContent>
    </Card>
  );
}
