"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { ArrowLeft, Download } from "lucide-react";
import { FootballShell } from "@/components/shell/app-shell";
import { PlayerPortrait } from "@/components/shared/player-portrait";
import { TrendLine } from "@/components/shared/trend-line";
import { fmtMetric, playerProfileHref } from "@/components/players-view";
import { apiPlayerToSummary, useAppState } from "@/lib/app-state";
import { fetchPlayer } from "@/lib/api";
import type { PlayerSummary } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { NativeSelect } from "@/components/ui/native-select";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState } from "@/components/composite/empty-state";
import { StatChip, StatLine } from "@/components/composite/stat-chip";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

type LoadState = "idle" | "loading" | "ready" | "missing" | "error";

export function PlayerProfileClient({ id }: { id: string }) {
  const router = useRouter();
  const { data, getPlayerById, setSelectedPlayerId, mockMode, authToken } = useAppState();
  const cached = getPlayerById(id);

  const [player, setPlayer] = useState<PlayerSummary | undefined>(cached);
  const [loadState, setLoadState] = useState<LoadState>(cached ? "ready" : "idle");
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  // Pull live identity from /api/v1/players/{id} when not in mock mode. The
  // cached entry from the roster fetch is shown immediately as a fast path;
  // the backend response overrides it on success.
  useEffect(() => {
    if (mockMode) {
      setPlayer(cached);
      setLoadState(cached ? "ready" : "missing");
      return;
    }
    const baseUrl = process.env.NEXT_PUBLIC_API_URL;
    if (!baseUrl) {
      if (cached) {
        setPlayer(cached);
        setLoadState("ready");
      } else {
        setErrorMessage("Not connected to the team server — player details are unavailable.");
        setLoadState("error");
      }
      return;
    }
    let cancelled = false;
    if (cached) setPlayer(cached);
    setLoadState(cached ? "ready" : "loading");
    (async () => {
      try {
        const apiPlayer = await fetchPlayer(id, authToken);
        if (cancelled) return;
        setPlayer(apiPlayerToSummary(apiPlayer));
        setLoadState("ready");
      } catch (err) {
        if (cancelled) return;
        const message = err instanceof Error ? err.message : String(err);
        if (/404/.test(message)) {
          setLoadState("missing");
        } else {
          setErrorMessage(message);
          setLoadState(cached ? "ready" : "error");
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [id, mockMode, authToken, cached]);

  if (loadState === "loading") {
    return (
      <FootballShell activePage="players">
        <div role="status" aria-busy="true" aria-label="Loading player" className="flex flex-col gap-3">
          <Skeleton className="h-32 w-full" />
          <Skeleton className="h-48 w-full" />
        </div>
      </FootballShell>
    );
  }

  if (loadState === "error") {
    return (
      <FootballShell activePage="players">
        <EmptyState
          title="Could not load player"
          hint={errorMessage ?? "An unexpected error occurred."}
          action={
            <Button asChild>
              <Link href="/players">
                <ArrowLeft className="size-4" /> Back to Roster
              </Link>
            </Button>
          }
        />
      </FootballShell>
    );
  }

  if (!player || loadState === "missing") {
    return (
      <FootballShell activePage="players">
        <EmptyState
          title="Player not found"
          hint={`We couldn't find a player with id ${id}.`}
          action={
            <Button asChild>
              <Link href="/players">
                <ArrowLeft className="size-4" /> Back to Roster
              </Link>
            </Button>
          }
        />
      </FootballShell>
    );
  }

  const others = data.players.filter((p) => p.id !== player.id);
  const metricsAvailable =
    player.maxSpeed != null || player.distance != null || player.separation != null;
  const trendAvailable = player.trend && player.trend.length > 0;
  const confidenceLabel =
    player.confidence != null ? `${Math.round(player.confidence * 100)}%` : "—";

  const exportProfile = () => {
    const lines = [
      `Player Profile — #${player.jersey} ${player.name}`,
      `Position: ${player.position} · Group: ${player.group}`,
      `Max Speed: ${player.maxSpeed != null ? `${player.maxSpeed} MPH` : "not available"}`,
      `Distance: ${player.distance != null ? `${player.distance} YDS` : "not available"}`,
      `Avg Separation: ${player.separation != null ? `${player.separation} YDS` : "not available"}`,
      `Identity Confidence: ${confidenceLabel}`,
      `Trend: ${trendAvailable ? player.trend!.join(", ") : "not available"}`,
    ];
    const blob = new Blob([lines.join("\n")], { type: "text/plain" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `player-${player.jersey}-${player.name.replace(/\s+/g, "-")}.txt`;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };

  return (
    <FootballShell activePage="players">
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <Button variant="outline" size="sm" onClick={() => router.back()}>
          <ArrowLeft className="size-4" /> Back
        </Button>
        <div className="flex flex-wrap items-center gap-2">
          <NativeSelect
            value={player.id}
            onChange={(e) => {
              setSelectedPlayerId(e.target.value);
              router.push(playerProfileHref(e.target.value));
            }}
            aria-label="Switch player"
            className="max-w-64"
          >
            {data.players.map((p) => (
              <option key={p.id} value={p.id}>
                #{p.jersey} {p.name} · {p.position}
              </option>
            ))}
          </NativeSelect>
          <Button size="sm" onClick={exportProfile}>
            <Download className="size-4" /> Export Profile
          </Button>
        </div>
      </div>

      <div className="grid gap-4 lg:grid-cols-3">
        <Card>
          <CardHeader>
            <h2 className="font-display text-base font-semibold uppercase tracking-wide">
              Identity
            </h2>
          </CardHeader>
          <CardContent>
            <PlayerPortrait player={player} />
            <div className="mt-3 flex flex-col gap-1.5">
              <StatLine label="Jersey" value={`#${player.jersey}`} />
              <StatLine label="Name" value={player.name} />
              <StatLine label="Position" value={player.position} />
              <StatLine label="Group" value={player.group} />
              <StatLine label="Identity Confidence" value={confidenceLabel} />
            </div>
          </CardContent>
        </Card>

        <Card className="lg:col-span-2">
          <CardHeader>
            <h2 className="font-display text-base font-semibold uppercase tracking-wide">
              Performance Metrics
            </h2>
          </CardHeader>
          <CardContent>
            {metricsAvailable ? (
              <div className="grid grid-cols-2 gap-2 md:grid-cols-4">
                <StatChip label="Max Speed" value={fmtMetric(player.maxSpeed)} hint="MPH" />
                <StatChip label="Distance" value={fmtMetric(player.distance)} hint="YDS" />
                <StatChip label="Avg Separation" value={fmtMetric(player.separation)} hint="YDS" />
                <StatChip label="Identity" value={confidenceLabel} />
              </div>
            ) : (
              <p className="text-xs text-muted-foreground">
                Performance metrics are not wired to the live pipeline yet. They will surface once
                per-player tracking metrics land (#100).
              </p>
            )}
            <div className="mt-4">
              <h3 className="mb-1.5 font-display text-xs font-semibold uppercase tracking-widest text-muted-foreground">
                Trend
              </h3>
              <TrendLine data={player.trend} />
            </div>
          </CardContent>
        </Card>
      </div>

      <Card className="mt-4">
        <CardHeader>
          <h2 className="font-display text-base font-semibold uppercase tracking-wide">
            Position Group · Quick Switch
          </h2>
        </CardHeader>
        <CardContent>
          {others.length === 0 ? (
            <p className="text-xs text-muted-foreground">No other players on the roster yet.</p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Player</TableHead>
                  <TableHead>Pos</TableHead>
                  <TableHead className="text-right">Max speed</TableHead>
                  <TableHead className="text-right">Distance</TableHead>
                  <TableHead className="text-right">Identity</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {others.map((p) => (
                  <TableRow key={p.id}>
                    <TableCell className="font-medium">
                      <Link
                        href={playerProfileHref(p.id)}
                        className="flex items-center gap-2 hover:text-primary"
                      >
                        <span data-numeric className="font-mono text-muted-foreground">
                          #{p.jersey}
                        </span>
                        {p.name}
                      </Link>
                    </TableCell>
                    <TableCell className="text-muted-foreground">{p.position}</TableCell>
                    <TableCell data-numeric className="text-right font-mono text-xs">
                      {fmtMetric(p.maxSpeed)} MPH
                    </TableCell>
                    <TableCell data-numeric className="text-right font-mono text-xs">
                      {fmtMetric(p.distance)} YDS
                    </TableCell>
                    <TableCell data-numeric className="text-right font-mono text-xs">
                      {p.confidence != null ? `${Math.round(p.confidence * 100)}%` : "—"}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </FootballShell>
  );
}
