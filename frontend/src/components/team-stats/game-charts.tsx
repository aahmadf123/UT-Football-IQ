"use client";

/**
 * Team Stats charts for one game and one side of the ball.
 *
 * Built from the summary's play log (every counted snap in game order), so
 * every chart can open the plays behind it. Successful plays are the
 * highlighted series and the rest recede; run/pass and offense/defense keep
 * their colours wherever they appear.
 */

import Link from "next/link";
import { useRouter } from "next/navigation";
import type { ReactNode } from "react";
import type { ApiPlayDetail, ApiSideSummary } from "@/lib/api";
import { cn } from "@/lib/utils";
import { BarList } from "@/components/charts/bar-list";
import { ColumnChart } from "@/components/charts/column-chart";
import { LineChart, type LineSeries } from "@/components/charts/line-chart";
import { SERIES_COLORS } from "@/components/charts/palette";

const ORDINAL = ["1st", "2nd", "3rd", "4th"];
const SUCCESS_COLOR = SERIES_COLORS[0];
const MISS_COLOR = "color-mix(in oklch, var(--muted-foreground) 55%, transparent)";
export const RUN_COLOR = SERIES_COLORS[0];
export const PASS_COLOR = SERIES_COLORS[1];
const OFFENSE_COLOR = SERIES_COLORS[0];
const DEFENSE_COLOR = SERIES_COLORS[2];

/** Clip review URL that steps through exactly these plays. */
export function playlistHref(clipIds: string[]): string {
  const params = new URLSearchParams({ clipId: clipIds[0], playlist: clipIds.join(",") });
  return `/clip-review/?${params.toString()}`;
}

/** Yards-gained buckets a coach reads: losses, stuffed, chunks, explosives. */
export const YARD_BUCKETS: { key: string; label: string; test: (yards: number) => boolean }[] = [
  { key: "loss", label: "Loss", test: (y) => y < 0 },
  { key: "0-3", label: "0–3", test: (y) => y >= 0 && y <= 3 },
  { key: "4-6", label: "4–6", test: (y) => y >= 4 && y <= 6 },
  { key: "7-9", label: "7–9", test: (y) => y >= 7 && y <= 9 },
  { key: "10-19", label: "10–19", test: (y) => y >= 10 && y <= 19 },
  { key: "20+", label: "20+", test: (y) => y >= 20 },
];

function downDistance(play: ApiPlayDetail): string | null {
  return play.down == null ? null : `${ORDINAL[play.down - 1]} & ${play.distance ?? "?"}`;
}

export function playTooltip(play: ApiPlayDetail): string[] {
  const kind = play.kind === "other" ? "" : ` ${play.kind}`;
  const head = `${play.yards_gained > 0 ? "+" : ""}${play.yards_gained} yd${kind}`;
  const context = [
    play.period ? (play.period === 5 ? "OT" : `Q${play.period}`) : null,
    downDistance(play),
    play.success == null ? null : play.success ? "successful" : "not successful",
  ]
    .filter(Boolean)
    .join(" · ");
  const lines = [head, context];
  if (play.epa != null) lines.push(`EPA ${play.epa > 0 ? "+" : ""}${play.epa.toFixed(2)}`);
  if (play.text) lines.push(play.text.length > 110 ? `${play.text.slice(0, 107)}…` : play.text);
  return lines.filter(Boolean);
}

export function ChartCard({ title, subtitle, children, className }: { title: string; subtitle?: string; children: ReactNode; className?: string }) {
  return (
    <section aria-label={title} className={cn("min-w-0 rounded-lg border border-border-soft p-4", className)}>
      <h3 className="text-sm font-semibold">{title}</h3>
      {subtitle && <p className="mt-0.5 text-xs text-muted-foreground">{subtitle}</p>}
      <div className="mt-3">{children}</div>
    </section>
  );
}

function Swatch({ color, label, line = false }: { color: string; label: string; line?: boolean }) {
  return (
    <li className="flex items-center gap-1.5">
      <span aria-hidden className={line ? "inline-block h-0.5 w-4 rounded" : "inline-block size-2.5 rounded-sm"} style={{ background: color }} />
      {label}
    </li>
  );
}

export function PlayByPlay({ log }: { log: ApiPlayDetail[] }) {
  const router = useRouter();
  const ids = log.map((p) => p.clip_id);
  const dividers: { before: number; label: string }[] = [];
  log.forEach((play, i) => {
    if (play.period != null && (i === 0 || log[i - 1].period !== play.period)) {
      dividers.push({ before: i, label: play.period === 5 ? "OT" : `Q${play.period}` });
    }
  });
  return (
    <ChartCard title="Play by play" subtitle="Yards gained on every snap, in game order. Successful plays are highlighted; click a play to watch from there.">
      <ColumnChart
        ariaLabel="Yards gained per play"
        formatY={(v) => String(v)}
        height={240}
        dividers={dividers}
        onColumnClick={(i) => router.push(playlistHref([...ids.slice(i), ...ids.slice(0, i)]))}
        columns={log.map((play) => ({
          key: play.clip_id,
          value: play.yards_gained,
          color: play.success ? SUCCESS_COLOR : MISS_COLOR,
          tooltip: playTooltip(play),
          marker: play.scoring ? "TD" : play.turnover ? "TO" : undefined,
        }))}
        legend={
          <ul className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground" aria-label="Legend">
            <Swatch color={SUCCESS_COLOR} label="Successful" />
            <Swatch color={MISS_COLOR} label="Not successful" />
            <li>
              <span className="font-bold text-foreground">TD</span> touchdown · <span className="font-bold text-foreground">TO</span> turnover
            </li>
          </ul>
        }
      />
    </ChartCard>
  );
}

export function GameFlow({ offense, defense }: { offense: ApiPlayDetail[]; defense: ApiPlayDetail[] }) {
  const cumulative = (log: ApiPlayDetail[]) => {
    let total = 0;
    return log
      .filter((p) => p.play_number != null)
      .map((p) => {
        total += p.epa ?? 0;
        return { x: p.play_number as number, y: Number(total.toFixed(2)) };
      });
  };
  const series: LineSeries[] = [
    { key: "offense", label: "Offense", color: OFFENSE_COLOR, points: cumulative(offense) },
    { key: "defense", label: "Defense (allowed)", color: DEFENSE_COLOR, points: cumulative(defense) },
  ].filter((s) => s.points.length);
  if (!series.length) return null;
  return (
    <ChartCard title="Game flow" subtitle="Expected points added, running total through the game. Offense rising is good; what the defense allowed rising is bad.">
      <LineChart
        series={series}
        ariaLabel="Cumulative EPA through the game"
        formatY={(v) => (v > 0 ? `+${v.toFixed(0)}` : v.toFixed(0))}
        formatX={(x) => `Play ${x}`}
        height={220}
        zeroBaseline
      />
    </ChartCard>
  );
}

export function YardsDistribution({ log }: { log: ApiPlayDetail[] }) {
  const router = useRouter();
  const buckets = YARD_BUCKETS.map((bucket) => ({ ...bucket, plays: log.filter((p) => bucket.test(p.yards_gained)) }));
  return (
    <ChartCard title="Yards per play" subtitle="How often plays are stuffed, move the chains or break big. Click a bar to watch those plays.">
      <ColumnChart
        ariaLabel="Plays by yards gained"
        formatY={(v) => String(v)}
        height={220}
        onColumnClick={(i) => {
          if (buckets[i].plays.length) router.push(playlistHref(buckets[i].plays.map((p) => p.clip_id)));
        }}
        columns={buckets.map((bucket) => ({
          key: bucket.key,
          value: bucket.plays.length,
          color: SERIES_COLORS[0],
          tick: bucket.label,
          capLabel: String(bucket.plays.length),
          tooltip: [
            `${bucket.plays.length} plays`,
            `${bucket.label} yards`,
            log.length ? `${Math.round((bucket.plays.length / log.length) * 100)}% of snaps` : "",
          ].filter(Boolean),
        }))}
      />
    </ChartCard>
  );
}

export function ByDown({ side }: { side: ApiSideSummary }) {
  return (
    <ChartCard title="By down" subtitle="Success rate on each down, and how often it's a run.">
      <BarList
        ariaLabel="Success rate by down"
        color={SERIES_COLORS[0]}
        max={1}
        format={(v) => `${Math.round(v * 100)}%`}
        rows={side.by_down.map((d) => ({
          key: String(d.down),
          label: `${ORDINAL[d.down - 1]} down`,
          value: d.success_rate,
          note: (
            <Link href={playlistHref(d.clip_ids)} aria-label={`${ORDINAL[d.down - 1]} down plays`} className="font-normal text-muted-foreground hover:text-primary">
              {d.plays} plays
            </Link>
          ),
        }))}
      />
      <p className="mb-2 mt-5 text-xs font-medium">Run / pass mix</p>
      <ul className="flex flex-col gap-2" aria-label="Run share by down">
        {side.by_down.map((d) => {
          const run = d.rush_share ?? 0;
          return (
            <li key={d.down} className="grid grid-cols-[4.5rem_minmax(0,1fr)_5.5rem] items-center gap-3 text-xs">
              <span className="text-muted-foreground">{ORDINAL[d.down - 1]} down</span>
              <span className="flex h-3 gap-[2px]" title={`${Math.round(run * 100)}% run, ${Math.round((1 - run) * 100)}% pass`}>
                {run > 0 && <span className="h-full rounded-l" style={{ width: `${run * 100}%`, background: RUN_COLOR }} />}
                {run < 1 && <span className="h-full rounded-r" style={{ width: `${(1 - run) * 100}%`, background: PASS_COLOR }} />}
              </span>
              <span className="text-right" style={{ fontVariantNumeric: "tabular-nums" }}>{Math.round(run * 100)}% run</span>
            </li>
          );
        })}
      </ul>
      <ul className="mt-2 flex gap-4 text-xs text-muted-foreground" aria-label="Legend">
        <Swatch color={RUN_COLOR} label="Run" />
        <Swatch color={PASS_COLOR} label="Pass" />
      </ul>
    </ChartCard>
  );
}

export function RunVsPass({ side }: { side: ApiSideSummary }) {
  const groups = Object.fromEntries(side.groups.map((g) => [g.key, g]));
  const rows = [
    { label: "Yards per play", run: side.rush_yards_per_play, pass: side.pass_yards_per_play, fmt: (v: number) => v.toFixed(1) },
    { label: "Success rate", run: side.rush_success_rate, pass: side.pass_success_rate, fmt: (v: number) => `${Math.round(v * 100)}%` },
    { label: "Plays", run: side.rush_plays, pass: side.pass_plays, fmt: (v: number) => String(v) },
    { label: "Yards", run: side.rush_yards, pass: side.pass_yards, fmt: (v: number) => String(v) },
  ];
  return (
    <ChartCard title="Run vs pass" subtitle="Which way the ball moved, and how well.">
      <ul className="mb-3 flex gap-4 text-xs text-muted-foreground" aria-label="Legend">
        <Swatch color={RUN_COLOR} label="Run" />
        <Swatch color={PASS_COLOR} label="Pass" />
      </ul>
      <div className="flex flex-col gap-4">
        {rows.map((row) => {
          const max = Math.max(Math.abs(row.run ?? 0), Math.abs(row.pass ?? 0)) || 1;
          return (
            <div key={row.label}>
              <p className="mb-1 text-xs text-muted-foreground">{row.label}</p>
              {(
                [
                  ["Run", row.run, RUN_COLOR],
                  ["Pass", row.pass, PASS_COLOR],
                ] as const
              ).map(([name, value, color]) => (
                <div key={name} className="mb-[2px] grid grid-cols-[minmax(0,1fr)_3.5rem] items-center gap-2" title={`${name}: ${value != null ? row.fmt(value) : "—"}`}>
                  <span className="relative h-3">
                    {value != null && <span className="absolute inset-y-0 left-0 rounded-r" style={{ width: `${(Math.abs(value) / max) * 100}%`, background: color }} />}
                  </span>
                  <span className="text-right text-xs font-semibold" style={{ fontVariantNumeric: "tabular-nums" }}>{value != null ? row.fmt(value) : "—"}</span>
                </div>
              ))}
            </div>
          );
        })}
      </div>
      <div className="mt-4 flex flex-wrap gap-3 text-xs">
        {groups.rush && <Link href={playlistHref(groups.rush.clip_ids)} className="text-primary hover:underline">Watch the runs</Link>}
        {groups.pass && <Link href={playlistHref(groups.pass.clip_ids)} className="text-primary hover:underline">Watch the passes</Link>}
      </div>
    </ChartCard>
  );
}

export function ByQuarter({ log }: { log: ApiPlayDetail[] }) {
  const quarters = [...new Set(log.map((p) => p.period).filter((p): p is number => p != null))].sort((a, b) => a - b);
  if (!quarters.length) return null;
  const rows = quarters.map((q) => {
    const plays = log.filter((p) => p.period === q);
    const graded = plays.filter((p) => p.success != null);
    return {
      label: q === 5 ? "OT" : `Q${q}`,
      ypp: plays.length ? plays.reduce((n, p) => n + p.yards_gained, 0) / plays.length : null,
      success: graded.length ? graded.filter((p) => p.success).length / graded.length : null,
      plays: plays.length,
    };
  });
  const maxYpp = Math.max(1, ...rows.map((r) => r.ypp ?? 0));
  return (
    <ChartCard title="By quarter" subtitle="Did it hold up over four quarters?" className="lg:col-span-2">
      <div className="grid gap-6 md:grid-cols-2">
        <div>
          <p className="mb-2 text-xs font-medium">Yards per play</p>
          <BarList
            ariaLabel="Yards per play by quarter"
            color={SERIES_COLORS[0]}
            max={Math.ceil(maxYpp)}
            format={(v) => v.toFixed(1)}
            rows={rows.map((r) => ({ key: r.label, label: r.label, value: r.ypp, note: <span className="font-normal text-muted-foreground">{r.plays} plays</span> }))}
          />
        </div>
        <div>
          <p className="mb-2 text-xs font-medium">Success rate</p>
          <BarList
            ariaLabel="Success rate by quarter"
            color={SERIES_COLORS[0]}
            max={1}
            format={(v) => `${Math.round(v * 100)}%`}
            rows={rows.map((r) => ({ key: r.label, label: r.label, value: r.success }))}
          />
        </div>
      </div>
    </ChartCard>
  );
}
