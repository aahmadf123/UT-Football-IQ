"use client";

import { useEffect, useState } from "react";
import { friendlyMessage } from "@/lib/api-error";
import { fetchPlayerCollegeSeasons, type CfbdPlayerSeason } from "@/lib/cfbd-admin";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { NativeSelect } from "@/components/ui/native-select";
import { Skeleton } from "@/components/ui/skeleton";
import { StatChip } from "@/components/composite/stat-chip";

/** CFBD stat categories in the order a coach reads a box score. */
const CATEGORY_ORDER = [
  "passing",
  "rushing",
  "receiving",
  "defensive",
  "interceptions",
  "fumbles",
  "kicking",
  "punting",
  "kickReturns",
  "puntReturns",
];

const CATEGORY_LABELS: Record<string, string> = {
  passing: "Passing",
  rushing: "Rushing",
  receiving: "Receiving",
  defensive: "Defense",
  interceptions: "Interceptions",
  fumbles: "Fumbles",
  kicking: "Kicking",
  punting: "Punting",
  kickReturns: "Kick returns",
  puntReturns: "Punt returns",
};

function pct(value: number | null | undefined): string {
  return value == null ? "—" : `${Math.round(value * 1000) / 10}%`;
}

function num(value: number | null | undefined, digits = 2): string {
  return value == null ? "—" : value.toFixed(digits);
}

function categories(season: CfbdPlayerSeason): [string, [string, string | number | null][]][] {
  const stats = season.stats ?? {};
  const keys = Object.keys(stats).sort((a, b) => {
    const ia = CATEGORY_ORDER.indexOf(a);
    const ib = CATEGORY_ORDER.indexOf(b);
    return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib) || a.localeCompare(b);
  });
  return keys.map((k) => [k, Object.entries(stats[k] ?? {})]);
}

/**
 * Official season production from CollegeFootballData: box-score stats, the
 * share of team plays the player was involved in (usage), and predicted
 * points added (PPA, CFBD's EPA). Sits beside our film-tracking metrics.
 */
export function CollegeStatsCard({ playerId, token }: { playerId: string; token?: string }) {
  const [seasons, setSeasons] = useState<CfbdPlayerSeason[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<number | null>(null);

  useEffect(() => {
    let cancelled = false;
    setSeasons(null);
    setError(null);
    fetchPlayerCollegeSeasons(playerId, token)
      .then((res) => {
        if (cancelled) return;
        setSeasons(res.seasons);
        setSelected(res.seasons[0]?.season ?? null);
      })
      .catch((err) => {
        if (!cancelled) setError(friendlyMessage(err, "load college stats"));
      });
    return () => {
      cancelled = true;
    };
  }, [playerId, token]);

  const season = seasons?.find((s) => s.season === selected) ?? null;

  return (
    <Card className="mt-4">
      <CardHeader className="flex flex-row flex-wrap items-center justify-between gap-2">
        <h2 className="font-display text-base font-semibold uppercase tracking-wide">
          College stats
        </h2>
        {seasons && seasons.length > 1 ? (
          <NativeSelect
            aria-label="Season"
            value={selected ?? ""}
            onChange={(e) => setSelected(Number(e.target.value))}
            className="w-28"
          >
            {seasons.map((s) => (
              <option key={s.season} value={s.season}>
                {s.season}
              </option>
            ))}
          </NativeSelect>
        ) : null}
      </CardHeader>
      <CardContent>
        {error ? (
          <p role="alert" className="text-xs text-muted-foreground">
            {error}
          </p>
        ) : seasons === null ? (
          <Skeleton className="h-24 w-full" />
        ) : !season ? (
          <p className="text-xs text-muted-foreground">
            No official game stats yet. They appear after this player records stats in a game and
            the roster syncs (weekly, or Settings → College data → Sync roster).
          </p>
        ) : (
          <div className="flex flex-col gap-4">
            <div className="grid grid-cols-2 gap-2 md:grid-cols-4">
              <StatChip label="Usage" value={pct(season.usage?.overall)} hint="OF TEAM PLAYS" />
              <StatChip label="PPA / play" value={num(season.ppa?.average?.all)} hint="AVG" />
              <StatChip label="Total PPA" value={num(season.ppa?.total?.all, 1)} hint="SEASON" />
              <StatChip
                label="3rd down usage"
                value={pct(season.usage?.thirdDown)}
                hint="OF TEAM PLAYS"
              />
            </div>
            {categories(season).length === 0 ? (
              <p className="text-xs text-muted-foreground">No box-score stats for {season.season}.</p>
            ) : (
              <div className="grid gap-3 md:grid-cols-2">
                {categories(season).map(([category, entries]) => (
                  <div key={category} className="rounded-md border p-3">
                    <h3 className="mb-2 font-display text-xs font-semibold uppercase tracking-widest text-muted-foreground">
                      {CATEGORY_LABELS[category] ?? category}
                    </h3>
                    <dl className="grid grid-cols-3 gap-x-3 gap-y-1.5 sm:grid-cols-4">
                      {entries.map(([stat, value]) => (
                        <div key={stat} className="flex flex-col">
                          <dt className="text-[10px] uppercase tracking-wide text-muted-foreground">
                            {stat}
                          </dt>
                          <dd data-numeric className="font-mono text-sm">
                            {value ?? "—"}
                          </dd>
                        </div>
                      ))}
                    </dl>
                  </div>
                ))}
              </div>
            )}
            <p className="text-[10px] text-muted-foreground">
              Source: CollegeFootballData.com · {season.team ?? "Toledo"} {season.season}
              {season.position ? ` · listed ${season.position}` : ""}
            </p>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
