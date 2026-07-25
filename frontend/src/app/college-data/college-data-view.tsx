"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
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

const SOURCE_LABEL = "CollegeFootballData.com";

const OFFLINE_REASON =
  "Cached CFBD analytics are unavailable offline — they load when the team server is connected.";

export function CollegeDataView() {
  const { authToken } = useAppState();
  const [team, setTeam] = useState<FetchState<CfbdTeamResponse>>({ kind: "loading" });
  const [schedule, setSchedule] = useState<FetchState<CfbdScheduleResponse>>({ kind: "loading" });
  const [benchmark, setBenchmark] = useState<FetchState<CfbdMacBenchmarkResponse>>({
    kind: "loading",
  });

  const apiConfigured = !!process.env.NEXT_PUBLIC_API_URL;

  const load = useCallback(async () => {
    if (!apiConfigured) {
      setTeam({ kind: "offline" });
      setSchedule({ kind: "offline" });
      setBenchmark({ kind: "offline" });
      return;
    }
    setTeam({ kind: "loading" });
    setSchedule({ kind: "loading" });
    setBenchmark({ kind: "loading" });

    const run = async <T,>(
      fn: () => Promise<T>,
      set: (s: FetchState<T>) => void,
    ): Promise<void> => {
      try {
        set({ kind: "ready", data: await fn() });
      } catch (err) {
        set({ kind: "error", message: err instanceof Error ? err.message : String(err) });
      }
    };

    await Promise.all([
      run(() => fetchCfbdToledoTeam(authToken), setTeam),
      run(() => fetchCfbdToledoSchedule(undefined, authToken), setSchedule),
      run(() => fetchCfbdMacBenchmark(undefined, authToken), setBenchmark),
    ]);
  }, [apiConfigured, authToken]);

  useEffect(() => {
    load();
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
    if (!apiConfigured) {
      return { tone: "unavailable", message: OFFLINE_REASON };
    }
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
  }, [apiConfigured, team, schedule, benchmark, caches]);

  return (
    <div className="flex flex-col gap-4">
      {banner && (
        <div
          data-testid="cfbd-banner"
          data-banner-tone={banner.tone}
          className={cn(
            "rounded-lg border px-3.5 py-2.5 text-[0.85rem]",
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
            empty: "No cached Toledo team record yet. Run the CFBD ingestion (Issues #161/#162).",
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
                  className="grid grid-cols-[74px_1fr_auto] items-baseline gap-2 text-[0.82rem]"
                >
                  <span className="text-xs text-muted-foreground">
                    {g.start_date ? formatDate(g.start_date) : "TBD"}
                  </span>
                  <span className="truncate font-medium">
                    {g.away_team} @ {g.home_team}
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
              <div className="grid grid-cols-[1fr_48px_64px_64px] gap-2 text-[0.65rem] font-semibold uppercase tracking-widest text-muted-foreground">
                <span>Team</span>
                <span>G</span>
                <span>Avg pts</span>
                <span>Diff</span>
              </div>
              {benchmark.data.teams.map((t) => (
                <div
                  key={t.team}
                  className="grid grid-cols-[1fr_48px_64px_64px] items-baseline gap-2 text-[0.82rem]"
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
            <h2 className="font-display text-sm font-semibold uppercase tracking-wide">
              Field Schematic
            </h2>
          </CardHeader>
          <CardContent>
            <FieldDiagram />
            <p className="mt-2 text-xs text-muted-foreground">
              Generic NCAA field rendered natively in SVG. Calibrated route overlays land with the
              tracking pipeline (#127/#128/#129).
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
    <div className="grid grid-cols-[100px_1fr] items-baseline gap-2 text-[0.82rem]">
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
