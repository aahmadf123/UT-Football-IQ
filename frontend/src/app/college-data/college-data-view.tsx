"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AnalyticsCard, type AnalyticsCardState } from "@/components/analytics-card";
import { FieldDiagram } from "@/components/field-diagram";
import { useAppState } from "@/lib/app-state";
import type { FetchState } from "@/lib/fetch-state";
import {
  fetchCfbdMacBenchmark,
  fetchCfbdToledoSchedule,
  fetchCfbdToledoTeam,
} from "@/lib/api";
import type {
  CfbdCacheMeta,
  CfbdMacBenchmarkResponse,
  CfbdScheduleResponse,
  CfbdTeamResponse,
} from "@/lib/types";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { cn } from "@/lib/utils";
import { Database, RefreshCw, ArrowRight, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { NativeSelect } from "@/components/ui/native-select";
import {
  fetchCfbdConnection, fetchCfbdGamePlays, refreshCfbdScope,
  type CfbdConnectionStatus, type CfbdGamePlays, type CfbdSyncScope,
} from "@/lib/cfbd-admin";
import { friendlyMessage } from "@/lib/api-error";

const SOURCE_LABEL = "CollegeFootballData.com";

const OFFLINE_REASON =
  "Cached CFBD analytics are unavailable offline — they load when the team server is connected.";

export function CollegeDataView() {
  const { authToken, currentRole } = useAppState();
  const [season, setSeason] = useState(new Date().getFullYear());
  const [seasonType, setSeasonType] = useState<"regular" | "postseason">("regular");
  const [week, setWeek] = useState(1);
  const [connection, setConnection] = useState<CfbdConnectionStatus | null>(null);
  const [syncing, setSyncing] = useState<string | null>(null);
  const [syncNotice, setSyncNotice] = useState<string | null>(null);
  const [syncError, setSyncError] = useState<string | null>(null);
  const [selectedGame, setSelectedGame] = useState<{ id: number; label: string } | null>(null);
  const [plays, setPlays] = useState<FetchState<CfbdGamePlays>>({ kind: "empty" });
  const loadGeneration = useRef(0);
  const [team, setTeam] = useState<FetchState<CfbdTeamResponse>>({ kind: "loading" });
  const [schedule, setSchedule] = useState<FetchState<CfbdScheduleResponse>>({ kind: "loading" });
  const [benchmark, setBenchmark] = useState<FetchState<CfbdMacBenchmarkResponse>>({
    kind: "loading",
  });

  const load = useCallback(async () => {
    const generation = ++loadGeneration.current;
    setTeam({ kind: "loading" });
    setSchedule({ kind: "loading" });
    setBenchmark({ kind: "loading" });

    const run = async <T,>(
      fn: () => Promise<T>,
      set: (s: FetchState<T>) => void,
    ): Promise<void> => {
      try {
        const data = await fn();
        if (generation === loadGeneration.current) set({ kind: "ready", data });
      } catch (err) {
        if (generation === loadGeneration.current) set({ kind: "error", message: friendlyMessage(err) });
      }
    };

    await Promise.all([
      run(() => fetchCfbdToledoTeam(authToken), setTeam),
      run(() => fetchCfbdToledoSchedule(season, authToken), setSchedule),
      run(() => fetchCfbdMacBenchmark(season, authToken), setBenchmark),
    ]);
  }, [authToken, season]);

  useEffect(() => {
    let cancelled = false;
    fetchCfbdConnection(authToken).then((value) => {
      if (!cancelled && typeof value.configured === "boolean") setConnection(value);
    }).catch(() => { if (!cancelled) setConnection(null); });
    return () => { cancelled = true; };
  }, [authToken]);

  useEffect(() => {
    if (!selectedGame) return;
    let cancelled = false;
    setPlays({ kind: "loading" });
    fetchCfbdGamePlays(selectedGame.id, authToken).then((data) => {
      if (!cancelled) setPlays({ kind: "ready", data });
    }).catch((error: unknown) => {
      if (!cancelled) setPlays({ kind: "error", message: friendlyMessage(error) });
    });
    return () => { cancelled = true; };
  }, [selectedGame, authToken, syncNotice]);

  async function sync(scopes: CfbdSyncScope[]) {
    setSyncError(null); setSyncNotice(null);
    let count = 0;
    try {
      for (const scope of scopes) {
        setSyncing(scope === "plays" ? `week ${week} play-by-play` : scope);
        const result = await refreshCfbdScope({ season, season_type: seasonType, scope, ...(scope === "plays" ? { week } : {}) }, authToken);
        count += result.rows_written;
        if (result.status !== "succeeded") throw new Error(result.message);
      }
      setSyncNotice(`Updated ${count} cached records for ${season}.`);
    } catch (error) {
      setSyncError(friendlyMessage(error));
    } finally {
      setSyncing(null);
      await load();
    }
  }

  useEffect(() => {
    void load();
    return () => { loadGeneration.current += 1; };
  }, [load]);

  // Aggregate cache freshness across whichever responses have loaded.
  const caches = useMemo<CfbdCacheMeta[]>(() => {
    const out: CfbdCacheMeta[] = [];
    if (team.kind === "ready") out.push(team.data.cache);
    if (schedule.kind === "ready") out.push(schedule.data.cache);
    if (benchmark.kind === "ready") out.push(benchmark.data.cache);
    return out;
  }, [team, schedule, benchmark]);

  const banner = useMemo<BannerInfo | null>(() => {
    const anyError =
      team.kind === "error" || schedule.kind === "error" || benchmark.kind === "error";
    const syncError = caches.some((c) => c.sync_status === "error");
    if (anyError || syncError) {
      return {
        tone: "unavailable",
        message:
          "CFBD data is currently unavailable. Showing the last successfully cached data if any exists.",
      };
    }
    const lastSync = caches
      .map((c) => c.last_synced_at)
      .filter((v): v is string => !!v)
      .sort()
      .at(-1);
    // Only warn about staleness when data was actually synced before but is now
    // old. A never-synced (empty) cache is communicated by the per-card empty
    // states, not a misleading "out of date" banner.
    if (caches.some((c) => c.stale && c.last_synced_at)) {
      return {
        tone: "stale",
        message: lastSync
          ? `Cached CFBD data may be out of date (last synced ${formatDate(lastSync)}).`
          : "Cached CFBD data may be out of date.",
      };
    }
    return null;
  }, [team, schedule, benchmark, caches]);

  return (
    <div className="flex flex-col gap-4">
      <section className="rounded-xl border border-border bg-card p-5" aria-label="College data connection">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="flex gap-3"><Database className="mt-1 size-5 text-primary" /><div>
            <h2 className="text-lg font-semibold">College football intelligence</h2>
            <p className="mt-1 max-w-2xl text-sm text-muted-foreground">Toledo schedules, conference benchmarks, and game context from CollegeFootballData.com.</p>
            <p role="status" className="mt-2 text-xs text-muted-foreground">{connection?.configured ? "CFBD connected · shared server cache" : connection?.configured === false ? "CFBD key is not configured on the team server. Cached data stays available." : "Connection status unavailable. Cached data can still be browsed."}</p>
          </div></div>
          <Button size="sm" variant="outline" onClick={() => void load()} disabled={syncing !== null}><RefreshCw className="size-4" /> Reload cached data</Button>
        </div>
        <div className="mt-5 flex flex-wrap items-end gap-3">
          <div><Label htmlFor="cfbd-season">Season</Label><NativeSelect id="cfbd-season" className="mt-2 w-28" value={season} disabled={syncing !== null} onChange={(event) => { setSeason(Number(event.target.value)); setSelectedGame(null); setSyncNotice(null); }}>{Array.from({ length: new Date().getFullYear() - 1998 }, (_, index) => new Date().getFullYear() + 1 - index).map((year) => <option key={year} value={year}>{year}</option>)}</NativeSelect></div>
          {currentRole === "admin" ? <>
            <div><Label htmlFor="cfbd-season-type">Season type</Label><NativeSelect id="cfbd-season-type" className="mt-2" value={seasonType} disabled={syncing !== null} onChange={(event) => setSeasonType(event.target.value as "regular" | "postseason")}><option value="regular">Regular season</option><option value="postseason">Postseason</option></NativeSelect></div>
            <Button disabled={!connection?.configured || syncing !== null} onClick={() => void sync(["teams", "schedule", "benchmark"])}><RefreshCw className={cn("size-4", syncing && "animate-spin")} />{syncing ? `Refreshing ${syncing}…` : "Refresh season data"}</Button>
            <div><Label htmlFor="cfbd-week">Week</Label><Input id="cfbd-week" type="number" min={1} max={20} className="mt-2 w-20" value={week} disabled={syncing !== null} onChange={(event) => setWeek(Math.min(20, Math.max(1, Number(event.target.value))))} /></div>
            <Button variant="outline" disabled={!connection?.configured || syncing !== null} onClick={() => void sync(["plays"])}>Refresh week’s plays</Button>
          </> : <p className="pb-2 text-xs text-muted-foreground">An administrator can refresh the shared CFBD cache.</p>}
        </div>
        {syncNotice ? <p role="status" className="mt-3 text-sm text-status-ok">{syncNotice}</p> : null}
        {syncError ? <p role="alert" className="mt-3 text-sm text-status-danger">{syncError}</p> : null}
      </section>
      {banner && (
        <div
          data-testid="cfbd-banner"
          data-banner-tone={banner.tone}
          className={cn(
            "rounded-lg border px-3.5 py-2.5 text-sm",
            banner.tone === "unavailable"
              ? "border-status-danger/50 bg-status-danger/10 text-foreground"
              : "border-primary/50 bg-primary/10 text-foreground",
          )}
        >
          {banner.message}
        </div>
      )}

      <div className="grid gap-4 lg:grid-cols-3">
        <AnalyticsCard
          title="Toledo Team"
          state={toCardState(team, (r) => (r.team ? "live" : "empty"), load, {
            empty: "No cached Toledo team record yet. An administrator can refresh season data above.",
          })}
        >
          {team.kind === "ready" && team.data.team && (
            <div className="flex flex-col gap-1">
              <Row label="School" value={team.data.team.school} />
              <Row label="Mascot" value={team.data.team.mascot ?? "—"} />
              <Row label="Conference" value={team.data.team.conference ?? "—"} />
              <Row label="Division" value={team.data.team.division ?? "—"} />
            </div>
          )}
        </AnalyticsCard>

        <AnalyticsCard
          title="Toledo Schedule"
          className="lg:col-span-2"
          state={toCardState(
            schedule,
            (r) => (r.games.length > 0 ? "live" : "empty"),
            load,
            { empty: "No cached games for Toledo yet." },
          )}
        >
          {schedule.kind === "ready" && (
            <div className="flex flex-col gap-1" data-testid="cfbd-schedule">
              {schedule.data.games.map((g) => (
                <div
                  key={g.cfbd_game_id}
                  className="grid grid-cols-[74px_1fr_auto] items-center gap-2 border-b border-border-soft py-3 text-sm last:border-0"
                >
                  <span className="text-xs text-muted-foreground">
                    {g.start_date ? formatDate(g.start_date) : "TBD"}
                  </span>
                  <span className="truncate font-medium">
                    <button type="button" onClick={() => setSelectedGame({ id: g.cfbd_game_id, label: `${g.away_team} @ ${g.home_team}` })} className="inline-flex max-w-full items-center gap-2 text-left hover:text-primary">{g.away_team} @ {g.home_team}<ArrowRight className="size-3 shrink-0" /></button>
                  </span>
                  <span data-numeric className="font-mono text-xs">
                    {g.home_points != null && g.away_points != null
                      ? `${g.away_points}–${g.home_points}`
                      : "—"}
                  </span>
                </div>
              ))}
            </div>
          )}
        </AnalyticsCard>
      </div>

      {selectedGame ? <Card>
        <CardHeader className="flex flex-row items-center justify-between gap-3"><div><h2 className="font-semibold">{selectedGame.label}</h2><p className="mt-1 text-xs text-muted-foreground">Cached play-by-play · external game context</p></div><Button size="icon" variant="ghost" aria-label="Close game plays" onClick={() => setSelectedGame(null)}><X className="size-4" /></Button></CardHeader>
        <CardContent>
          {plays.kind === "loading" ? <p role="status">Loading game plays…</p> : null}
          {plays.kind === "error" ? <p role="alert" className="text-sm text-status-danger">{plays.message}</p> : null}
          {plays.kind === "ready" && plays.data.plays.length === 0 ? <p className="text-sm text-muted-foreground">No cached plays for this game. An administrator can refresh its week above.</p> : null}
          {plays.kind === "ready" && plays.data.plays.length > 0 ? <div className="max-h-96 overflow-auto"><table className="w-full text-left text-sm"><thead className="sticky top-0 bg-card text-xs text-muted-foreground"><tr><th className="p-2">Quarter</th><th className="p-2">Situation</th><th className="p-2">Play</th><th className="p-2 text-right">Yards</th></tr></thead><tbody>{plays.data.plays.map((play) => <tr key={play.cfbd_play_id} className="border-t border-border-soft"><td className="p-2 tabular-nums">{play.period ?? "—"}</td><td className="whitespace-nowrap p-2">{play.down != null ? `${play.down} & ${play.distance ?? "—"}` : "—"}<span className="block text-xs text-muted-foreground">{play.offense ?? "Unknown offense"}</span></td><td className="p-2">{play.play_text || play.play_type || "Play details unavailable"}</td><td className="p-2 text-right tabular-nums">{play.yards_gained ?? "—"}</td></tr>)}</tbody></table></div> : null}
        </CardContent>
      </Card> : null}

      <div className="grid gap-4 lg:grid-cols-3">
        <AnalyticsCard
          title="MAC Benchmark (Points)"
          className="lg:col-span-2"
          state={toCardState(
            benchmark,
            (r) => (r.teams.length > 0 ? "live" : "empty"),
            load,
            { empty: "No cached MAC team game stats yet." },
          )}
        >
          {benchmark.kind === "ready" && (
            <div className="flex flex-col gap-1" data-testid="cfbd-benchmark">
              <div className="grid grid-cols-[1fr_48px_64px_64px] gap-2 text-[0.65rem] font-semibold text-muted-foreground">
                <span>Team</span>
                <span>G</span>
                <span>Avg pts</span>
                <span>Diff</span>
              </div>
              {benchmark.data.teams.map((t) => (
                <div
                  key={t.team}
                  className="grid grid-cols-[1fr_48px_64px_64px] items-baseline gap-2 text-sm"
                >
                  <span className="truncate font-medium">{t.team}</span>
                  <span data-numeric className="font-mono text-xs" title="Games">
                    {t.games}
                  </span>
                  <span data-numeric className="font-mono text-xs" title="Avg points for">
                    {fmtNum(t.avg_points_for)}
                  </span>
                  <span data-numeric className="font-mono text-xs" title="Point differential">
                    {fmtSigned(t.point_differential)}
                  </span>
                </div>
              ))}
            </div>
          )}
        </AnalyticsCard>

        <Card data-testid="cfbd-field-sample">
          <CardHeader>
            <h2 className="text-sm font-semibold">
              Field Schematic
            </h2>
          </CardHeader>
          <CardContent>
            <FieldDiagram />
            <p className="mt-2 text-xs text-muted-foreground">
              NCAA field dimensions for reference. View calibrated tracking overlays alongside your film in Clip Review.
            </p>
          </CardContent>
        </Card>
      </div>

      <p className="text-right text-xs text-muted-foreground" data-testid="cfbd-source-label">
        Source: {SOURCE_LABEL}. CFBD data is external context and is not derived from Toledo film.
      </p>
    </div>
  );
}

interface BannerInfo {
  tone: "stale" | "unavailable";
  message: string;
}

function toCardState<T>(
  state: FetchState<T>,
  liveOrEmpty: (resp: T) => "live" | "empty",
  retry: () => void,
  copy: { empty: string },
): AnalyticsCardState {
  switch (state.kind) {
    case "loading":
      return { kind: "loading", label: "Loading cached CFBD data…" };
    case "offline":
      return { kind: "unavailable", reason: OFFLINE_REASON };
    case "error":
      return { kind: "error", message: state.message, onRetry: retry };
    case "empty":
      return { kind: "empty", reason: copy.empty };
    case "ready":
      return liveOrEmpty(state.data) === "live"
        ? { kind: "live" }
        : { kind: "empty", reason: copy.empty };
  }
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="grid grid-cols-[100px_1fr] items-baseline gap-2 text-sm">
      <span className="text-xs text-muted-foreground">{label}</span>
      <span className="font-medium">{value}</span>
    </div>
  );
}

function formatDate(value: string): string {
  try {
    return new Date(value).toLocaleDateString(undefined, {
      month: "short",
      day: "numeric",
      year: "numeric",
    });
  } catch {
    return value;
  }
}

function fmtNum(value: number | null): string {
  return value == null ? "—" : value.toFixed(1);
}

function fmtSigned(value: number | null): string {
  if (value == null) return "—";
  return value > 0 ? `+${value.toFixed(1)}` : value.toFixed(1);
}
