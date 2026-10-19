"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { friendlyMessage } from "@/lib/api-error";
import { fetchPhysicalLoads, type ApiPhysicalLoad } from "@/lib/api";
import { useAppState } from "@/lib/app-state";
import { playerProfileHref } from "@/components/players-view";
import { mph } from "@/components/physical-load-card";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { NativeSelect } from "@/components/ui/native-select";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState } from "@/components/composite/empty-state";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

interface LeaderboardMetric {
  key: string;
  label: string;
  unit: string;
  value: (load: ApiPhysicalLoad) => number | null;
  format: (value: number) => string;
}

export const LEADERBOARD_METRICS: LeaderboardMetric[] = [
  {
    key: "top_speed",
    label: "Top speed",
    unit: "mph",
    value: (l) => l.top_speed_yps,
    format: (v) => mph(v),
  },
  { key: "sprints", label: "Sprints", unit: "", value: (l) => l.sprints, format: String },
  {
    key: "high_speed_yards",
    label: "High-speed yards",
    unit: "yd",
    value: (l) => l.high_speed_yards,
    format: (v) => String(Math.round(v)),
  },
  {
    key: "yards",
    label: "Total distance",
    unit: "yd",
    value: (l) => l.yards,
    format: (v) => String(Math.round(v)),
  },
  {
    key: "yards_per_rep",
    label: "Distance per rep",
    unit: "yd",
    value: (l) => (l.reps ? l.yards / l.reps : null),
    format: (v) => v.toFixed(1),
  },
  {
    key: "accelerations",
    label: "Accelerations",
    unit: "",
    value: (l) => l.accelerations,
    format: String,
  },
  {
    key: "peak_accel",
    label: "Peak acceleration",
    unit: "yd/s²",
    value: (l) => l.peak_accel_yps2,
    format: (v) => v.toFixed(1),
  },
  {
    key: "get_off",
    label: "Get-off (first second)",
    unit: "yd",
    value: (l) => (l.get_off_reps ? l.get_off_yards : null),
    format: (v) => v.toFixed(1),
  },
];

/** Rank every measured player on one physical metric, within a position group. */
export function PhysicalLeaderboard() {
  const { data, authToken } = useAppState();
  const [loads, setLoads] = useState<ApiPhysicalLoad[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [metricKey, setMetricKey] = useState(LEADERBOARD_METRICS[0].key);
  const [group, setGroup] = useState("all");

  useEffect(() => {
    let cancelled = false;
    setLoads(null);
    setError(null);
    fetchPhysicalLoads(authToken ?? undefined)
      .then((rows) => {
        if (!cancelled) setLoads(rows);
      })
      .catch((err) => {
        if (!cancelled) setError(friendlyMessage(err, "load the leaderboard"));
      });
    return () => {
      cancelled = true;
    };
  }, [authToken]);

  const players = useMemo(() => new Map(data.players.map((p) => [p.id, p])), [data.players]);
  const groups = useMemo(
    () => [...new Set(data.players.map((p) => p.group).filter(Boolean))].sort(),
    [data.players],
  );
  const metric = LEADERBOARD_METRICS.find((m) => m.key === metricKey) ?? LEADERBOARD_METRICS[0];

  const ranked = useMemo(() => {
    if (!loads) return [];
    return loads
      .map((load) => ({ load, player: players.get(load.player_id), value: metric.value(load) }))
      .filter(
        (row): row is typeof row & { value: number } =>
          row.value != null && (group === "all" || row.player?.group === group),
      )
      .sort((a, b) => b.value - a.value);
  }, [loads, players, metric, group]);

  return (
    <Card>
      <CardHeader className="flex flex-row flex-wrap items-center justify-between gap-2">
        <h2 className="font-display text-base font-semibold uppercase tracking-wide">
          Physical leaderboard
        </h2>
        <div className="flex flex-wrap gap-2">
          <NativeSelect
            aria-label="Metric"
            value={metricKey}
            onChange={(e) => setMetricKey(e.target.value)}
          >
            {LEADERBOARD_METRICS.map((m) => (
              <option key={m.key} value={m.key}>
                {m.label}
              </option>
            ))}
          </NativeSelect>
          <NativeSelect
            aria-label="Position group"
            value={group}
            onChange={(e) => setGroup(e.target.value)}
          >
            <option value="all">All positions</option>
            {groups.map((g) => (
              <option key={g} value={g}>
                {g}
              </option>
            ))}
          </NativeSelect>
        </div>
      </CardHeader>
      <CardContent>
        {error ? (
          <p role="alert" className="text-sm text-status-danger">{error}</p>
        ) : loads === null ? (
          <Skeleton className="h-40 w-full" />
        ) : ranked.length === 0 ? (
          <EmptyState
            title="No measured reps yet"
            hint="Players appear here once their film is processed and their tracks are identified."
          />
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-10">#</TableHead>
                <TableHead>Player</TableHead>
                <TableHead>Pos</TableHead>
                <TableHead className="text-right">
                  {metric.label}
                  {metric.unit ? ` (${metric.unit})` : ""}
                </TableHead>
                <TableHead className="text-right">Reps</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {ranked.map(({ load, player, value }, index) => (
                <TableRow key={load.player_id}>
                  <TableCell data-numeric className="font-mono text-muted-foreground">
                    {index + 1}
                  </TableCell>
                  <TableCell className="font-medium">
                    <Link href={playerProfileHref(load.player_id)} className="hover:text-primary">
                      {player ? `#${player.jersey} ${player.name}` : "Unknown player"}
                    </Link>
                  </TableCell>
                  <TableCell className="text-muted-foreground">{player?.position ?? "—"}</TableCell>
                  <TableCell data-numeric className="text-right font-mono">
                    {metric.format(value)}
                  </TableCell>
                  <TableCell data-numeric className="text-right font-mono text-muted-foreground">
                    {load.reps}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </CardContent>
    </Card>
  );
}
