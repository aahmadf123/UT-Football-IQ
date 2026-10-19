"use client";

import { useEffect, useState } from "react";
import { friendlyMessage } from "@/lib/api-error";
import { fetchPlayerPhysical, type ApiPlayerPhysicalDetail } from "@/lib/api";
import { YDS_PER_SEC_TO_MPH } from "@/lib/app-state";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { StatChip } from "@/components/composite/stat-chip";
import { TrendLine } from "@/components/shared/trend-line";

/** Speed bands in the order they are drawn, slowest first. */
export const SPEED_BANDS = [
  { key: "walk", label: "Walk", range: "< 4 mph", className: "bg-muted-foreground/30" },
  { key: "jog", label: "Jog", range: "4–8 mph", className: "bg-sky-500/60" },
  { key: "run", label: "Run", range: "8–11 mph", className: "bg-emerald-500/70" },
  { key: "high_speed", label: "High speed", range: "11–14 mph", className: "bg-amber-500/80" },
  { key: "sprint", label: "Sprint", range: "14+ mph", className: "bg-rose-500/90" },
] as const;

export function mph(yps: number | null | undefined): string {
  return yps == null ? "—" : (yps * YDS_PER_SEC_TO_MPH).toFixed(1);
}

function yards(value: number | null | undefined): string {
  return value == null ? "—" : String(Math.round(value));
}

/** Distance split across speed bands as one stacked bar with a legend. */
export function SpeedBandBar({ bands }: { bands: Record<string, number> }) {
  const total = SPEED_BANDS.reduce((sum, band) => sum + (bands[band.key] ?? 0), 0);
  if (total <= 0) return null;
  return (
    <div>
      <div
        className="flex h-3 w-full overflow-hidden rounded-full bg-secondary"
        role="img"
        aria-label={SPEED_BANDS.map(
          (band) => `${band.label} ${Math.round(bands[band.key] ?? 0)} yards`,
        ).join(", ")}
      >
        {SPEED_BANDS.map((band) => {
          const share = ((bands[band.key] ?? 0) / total) * 100;
          return share > 0 ? (
            <div key={band.key} className={band.className} style={{ width: `${share}%` }} />
          ) : null;
        })}
      </div>
      <ul className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
        {SPEED_BANDS.map((band) => (
          <li key={band.key} className="flex items-center gap-1.5">
            <span className={`inline-block size-2.5 rounded-sm ${band.className}`} aria-hidden />
            {band.label} <span className="text-muted-foreground/70">({band.range})</span>
            <span data-numeric className="font-mono text-foreground">
              {Math.round(bands[band.key] ?? 0)} yd
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

/**
 * Physical load from tracked film: distance by speed band, sprint and
 * acceleration efforts, top speed, and get-off (yards in the first second
 * after the snap). Totals add up over every tracked rep.
 */
export function PhysicalLoadCard({ playerId, token }: { playerId: string; token?: string }) {
  const [detail, setDetail] = useState<ApiPlayerPhysicalDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setDetail(null);
    setError(null);
    fetchPlayerPhysical(playerId, token)
      .then((res) => {
        if (!cancelled) setDetail(res);
      })
      .catch((err) => {
        if (!cancelled) setError(friendlyMessage(err, "load physical load"));
      });
    return () => {
      cancelled = true;
    };
  }, [playerId, token]);

  const load = detail?.load ?? null;

  return (
    <Card className="mt-4">
      <CardHeader>
        <h2 className="font-display text-base font-semibold uppercase tracking-wide">
          Physical load
        </h2>
      </CardHeader>
      <CardContent>
        {error ? (
          <p role="alert" className="text-sm text-status-danger">{error}</p>
        ) : detail === null ? (
          <Skeleton className="h-28 w-full" />
        ) : load === null ? (
          <p className="text-xs text-muted-foreground">
            No measured reps yet. Physical load appears once film with this player is processed
            and their track is identified.
          </p>
        ) : (
          <div className="flex flex-col gap-4">
            <div className="grid grid-cols-2 gap-2 md:grid-cols-4">
              <StatChip label="Top speed" value={mph(load.top_speed_yps)} hint="MPH" />
              <StatChip label="Distance" value={yards(load.yards)} hint="YDS" />
              <StatChip label="High-speed" value={yards(load.high_speed_yards)} hint="YDS" />
              <StatChip label="Sprints" value={load.sprints} hint="14+ MPH" />
              <StatChip label="Accelerations" value={load.accelerations} hint="> 3 M/S²" />
              <StatChip label="Decelerations" value={load.decelerations} hint="> 3 M/S²" />
              <StatChip
                label="Get-off"
                value={load.get_off_yards == null ? "—" : load.get_off_yards.toFixed(1)}
                hint={load.get_off_reps ? `YDS IN 1ST SEC · ${load.get_off_reps} REPS` : "NEEDS SNAP"}
              />
              <StatChip label="Reps" value={load.reps} hint="MEASURED" />
            </div>
            <div>
              <h3 className="mb-1.5 font-display text-xs font-semibold uppercase tracking-widest text-muted-foreground">
                Distance by speed
              </h3>
              <SpeedBandBar bands={load.band_yards} />
            </div>
            <div>
              <h3 className="mb-1.5 font-display text-xs font-semibold uppercase tracking-widest text-muted-foreground">
                Yards per film day
              </h3>
              <TrendLine
                data={detail.sessions.length >= 2 ? detail.sessions.map((s) => s.yards) : undefined}
              />
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
