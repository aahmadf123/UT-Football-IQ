"use client";

/**
 * Players roster view.
 *
 * Lists the live roster (identity from /api/v1/players via app-state) with a
 * client-side search and a local position-group filter (which replaced the old
 * global side-of-ball topbar select), plus a focus panel for the
 * hovered/selected player. Performance metrics that are not wired to the live
 * pipeline yet render "—" instead of fabricated numbers (#103).
 */

import Link from "next/link";
import { Search, UserRound } from "lucide-react";
import { useMemo, useState } from "react";
import { useAppState, type ApiStatus } from "@/lib/app-state";
import { PlayerPortrait } from "@/components/shared/player-portrait";
import type { PlayerSummary } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { NativeSelect } from "@/components/ui/native-select";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { StatChip } from "@/components/composite/stat-chip";

/** Canonical profile link — CSR detail page that works for any real id. */
export function playerProfileHref(id: string): string {
  return `/players/detail/?id=${encodeURIComponent(id)}`;
}

const ALL_GROUPS = "__all__";

export function PlayersView() {
  const { data, selectedPlayer, setSelectedPlayerId, playersStatus } = useAppState();
  const [query, setQuery] = useState("");
  const [group, setGroup] = useState<string>(ALL_GROUPS);

  const groups = useMemo(() => {
    const seen = new Set<string>();
    for (const p of data.players) seen.add(p.group);
    return [...seen].sort();
  }, [data.players]);

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    return data.players.filter((p) => {
      if (group !== ALL_GROUPS && p.group !== group) return false;
      if (!q) return true;
      return (
        p.name.toLowerCase().includes(q) ||
        p.position.toLowerCase().includes(q) ||
        p.jersey.includes(q)
      );
    });
  }, [data.players, query, group]);

  return (
    <div className="grid gap-4 lg:grid-cols-3">
      <Card className="lg:col-span-2">
        <CardHeader className="flex flex-row flex-wrap items-center justify-between gap-3">
          <h2 className="font-display text-base font-semibold uppercase tracking-wide">
            Roster Intelligence
          </h2>
          <div className="flex flex-wrap items-center gap-2">
            <NativeSelect
              value={group}
              onChange={(e) => setGroup(e.target.value)}
              aria-label="Filter by position group"
              data-testid="players-group-filter"
            >
              <option value={ALL_GROUPS}>All groups</option>
              {groups.map((g) => (
                <option key={g} value={g}>
                  {g}
                </option>
              ))}
            </NativeSelect>
            <div className="relative">
              <Search
                aria-hidden
                className="pointer-events-none absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground"
              />
              <Input
                className="h-9 w-56 pl-8"
                placeholder="Name, jersey, or position"
                aria-label="Search roster"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
              />
            </div>
          </div>
        </CardHeader>
        <CardContent>
          {visible.length === 0 ? (
            <p className="text-xs text-muted-foreground">
              {query.trim() || group !== ALL_GROUPS
                ? "No players match the current filter."
                : rosterEmptyMessage(playersStatus)}
            </p>
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
                {visible.map((player) => (
                  <TableRow
                    key={player.id}
                    className="cursor-pointer"
                    onMouseEnter={() => setSelectedPlayerId(player.id)}
                  >
                    <TableCell className="font-medium">
                      <Link
                        href={playerProfileHref(player.id)}
                        className="flex items-center gap-2 hover:text-primary"
                      >
                        <span data-numeric className="font-mono text-muted-foreground">
                          #{player.jersey}
                        </span>
                        {player.name}
                      </Link>
                    </TableCell>
                    <TableCell className="text-muted-foreground">{player.position}</TableCell>
                    <TableCell data-numeric className="text-right font-mono text-xs">
                      {fmtMetric(player.maxSpeed)} MPH
                    </TableCell>
                    <TableCell data-numeric className="text-right font-mono text-xs">
                      {fmtMetric(player.distance)} YDS
                    </TableCell>
                    <TableCell data-numeric className="text-right font-mono text-xs">
                      {fmtConfidence(player.confidence)}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardContent>
          {selectedPlayer ? (
            <>
              <PlayerFocus
                player={selectedPlayer}
                allPlayers={visible.length ? visible : data.players}
                onSelect={setSelectedPlayerId}
              />
              <Button asChild className="mt-4 w-full">
                <Link href={playerProfileHref(selectedPlayer.id)}>
                  <UserRound className="size-4" /> Open Full Profile
                </Link>
              </Button>
            </>
          ) : (
            <>
              <h2 className="font-display text-base font-semibold uppercase tracking-wide">
                Player Focus
              </h2>
              <p className="mt-2 text-xs text-muted-foreground">
                {rosterEmptyMessage(playersStatus)}
              </p>
            </>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

/**
 * Focus panel for one player: identity portrait + roster switcher + the
 * pipeline metrics (which render "—" until per-player tracking lands).
 * Shared by the Players and Player Development views.
 */
export function PlayerFocus({
  player,
  allPlayers,
  onSelect,
  compact = false,
}: {
  player: PlayerSummary;
  allPlayers: PlayerSummary[];
  onSelect: (id: string) => void;
  compact?: boolean;
}) {
  return (
    <>
      <div className="flex items-center justify-between gap-2">
        <h2 className="font-display text-base font-semibold uppercase tracking-wide">
          Player Focus
        </h2>
        <NativeSelect
          value={player.id}
          onChange={(e) => onSelect(e.target.value)}
          aria-label="Choose player"
          className="max-w-44"
        >
          {allPlayers.map((p) => (
            <option key={p.id} value={p.id}>
              #{p.jersey} {p.name}
            </option>
          ))}
        </NativeSelect>
      </div>
      <Link href={playerProfileHref(player.id)} className="mt-2 block">
        <PlayerPortrait player={player} compact={compact} />
      </Link>
      <div className={`grid grid-cols-3 gap-2 ${compact ? "mt-2" : "mt-3"}`}>
        <StatChip label="Distance" value={fmtMetric(player.distance)} hint="YDS" />
        <StatChip label="Max Speed" value={fmtMetric(player.maxSpeed)} hint="MPH" />
        <StatChip label="Avg Sep" value={fmtMetric(player.separation)} hint="YDS" />
      </div>
    </>
  );
}

export function rosterEmptyMessage(status: ApiStatus): string {
  switch (status) {
    case "loading":
      return "Loading roster…";
    case "offline":
      return "Roster unavailable — not connected to the team server.";
    case "live":
      return "Roster is empty. Add players from Settings to populate this view.";
    case "mock":
      return "No players yet.";
    default:
      return "No players yet.";
  }
}

// Render an optional numeric metric as a string. Metrics that aren't wired to
// the live backend yet (max speed, separation, distance) are surfaced as a
// dash rather than fabricated zeros — see Issue #103 acceptance criteria.
export function fmtMetric(value: number | undefined): string {
  return value == null ? "—" : String(value);
}

export function fmtConfidence(value: number | undefined): string {
  return value == null ? "—" : `${Math.round(value * 100)}%`;
}
