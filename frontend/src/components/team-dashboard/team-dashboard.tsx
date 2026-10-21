"use client";

/**
 * Home: how the team is performing, at a glance and one level deep.
 *
 *   1. Filters (season / 12 / 4 weeks; all film / practice / games) scope
 *      everything below them.
 *   2. Needs attention: film processing, waiting, plays filmed this week.
 *   3. Latest week of film vs the week before: reps, top speed, high-speed
 *      yards and sprints per rep, and the latest game's yards per play.
 *   4. Performance trend: weekly physical rates, practice and games.
 *   5. Position groups: each group's trend and change, for improvement.
 *   6. Games: the latest game's offense vs defense, and the season trend.
 *   7. Readiness: workload risk, under the health-data role rules.
 *
 * Every number is measured from film or the official play-by-play; empty
 * sections say what would fill them (repo rule #96).
 */

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useState } from "react";
import { AlertTriangle, ArrowRight, HeartPulse, Loader2, Upload } from "lucide-react";
import { fetchTeamDashboard, type ApiDashboardGame, type ApiGameSideSummary, type ApiTeamDashboard, type ApiTeamWeek } from "@/lib/api";
import { friendlyMessage } from "@/lib/api-error";
import { useAppState } from "@/lib/app-state";
import { useUploadWidget } from "@/components/shared/upload-widget";
import { mph } from "@/components/physical-load-card";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { EmptyState } from "@/components/composite/empty-state";
import { cn } from "@/lib/utils";
import { BarList } from "@/components/charts/bar-list";
import { LineChart, shortDate, type LineSeries } from "@/components/charts/line-chart";
import { SERIES_COLORS, SESSION_COLORS } from "@/components/charts/palette";
import { Sparkline, StatTile } from "@/components/charts/stat-tile";

type Range = { weeks: number; label: string };
const RANGES: Range[] = [
  { weeks: 52, label: "Season" },
  { weeks: 12, label: "12 weeks" },
  { weeks: 4, label: "4 weeks" },
];
type FilmFilter = "all" | "practice" | "game";
const FILM_FILTERS: { key: FilmFilter; label: string }[] = [
  { key: "all", label: "All film" },
  { key: "practice", label: "Practice" },
  { key: "game", label: "Games" },
];
const SESSION_LABEL: Record<string, string> = { practice: "Practice", game: "Games" };
const GROUP_ORDER = ["QB", "RB", "WR", "TE", "SKILL", "OL", "DL", "LB", "DB", "ST"];
const RISK_FLAG = 0.6;
const OFFENSE_COLOR = SERIES_COLORS[0];
const DEFENSE_COLOR = SERIES_COLORS[2];

const yd = (v: number) => v.toFixed(1);
const pct = (v: number) => `${Math.round(v * 100)}%`;
const epa = (v: number) => (v > 0 ? "+" : "") + v.toFixed(2);
const mphNum = (v: number) => mph(v);
const day = (iso: string) => Date.parse(`${iso.slice(0, 10)}T00:00:00Z`);

/** One row per week; practice and games combine, weighted by reps. */
export function combineWeeks(rows: ApiTeamWeek[]): ApiTeamWeek[] {
  const byWeek = new Map<string, ApiTeamWeek[]>();
  for (const row of rows) byWeek.set(row.week_start, [...(byWeek.get(row.week_start) ?? []), row]);
  return [...byWeek.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([week, group]) => {
      const reps = group.reduce((n, r) => n + r.reps, 0);
      const weighted = (key: keyof ApiTeamWeek) => {
        const parts = group.filter((r) => r[key] != null && r.reps > 0);
        const weight = parts.reduce((n, r) => n + r.reps, 0);
        return weight ? parts.reduce((n, r) => n + (r[key] as number) * r.reps, 0) / weight : null;
      };
      return {
        week_start: week,
        session_kind: group.length > 1 ? "all" : group[0].session_kind,
        reps,
        players: Math.max(...group.map((r) => r.players)),
        yards_per_rep: weighted("yards_per_rep"),
        high_speed_yards_per_rep: weighted("high_speed_yards_per_rep"),
        sprints_per_rep: weighted("sprints_per_rep"),
        top_speed_p90_yps: weighted("top_speed_p90_yps"),
        get_off_yards: weighted("get_off_yards"),
      };
    });
}

function change(latest: number | null | undefined, previous: number | null | undefined): number | null {
  return latest != null && previous != null ? latest - previous : null;
}

export function TeamDashboard() {
  const { authToken } = useAppState();
  const token = authToken ?? undefined;
  const [range, setRange] = useState(RANGES[0]);
  const [film, setFilm] = useState<FilmFilter>("all");
  const [data, setData] = useState<ApiTeamDashboard | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const { openFilePicker, widget } = useUploadWidget();

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    fetchTeamDashboard({ weeks: range.weeks, sessionKind: film === "all" ? null : film }, token)
      .then((result) => {
        if (cancelled) return;
        setData(result);
        setError(null);
      })
      .catch((err) => {
        if (!cancelled) setError(friendlyMessage(err, "load team performance"));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [range, film, token]);

  return (
    <>
      {widget}
      <div className="flex flex-col gap-5">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div role="group" aria-label="Dashboard filters" className="flex flex-wrap items-center gap-2">
            <Segmented label="Time range" options={RANGES.map((r) => ({ key: String(r.weeks), label: r.label }))} value={String(range.weeks)} onChange={(key) => setRange(RANGES.find((r) => String(r.weeks) === key) ?? RANGES[0])} />
            <Segmented label="Film" options={FILM_FILTERS} value={film} onChange={(key) => setFilm(key as FilmFilter)} />
            {loading && data && <Loader2 aria-label="Updating" className="size-4 animate-spin text-muted-foreground" />}
          </div>
          <div className="flex gap-2">
            <Button asChild variant="outline"><Link href="/film-room/">Open Film Room <ArrowRight className="size-4" /></Link></Button>
            <Button onClick={openFilePicker}><Upload className="size-4" /> Upload film</Button>
          </div>
        </div>

        {error && !data ? (
          <EmptyState title="Team performance didn't load" hint={error} />
        ) : !data ? (
          <div className="flex items-center gap-2 py-16 text-sm text-muted-foreground"><Loader2 className="size-4 animate-spin" /> Loading team performance…</div>
        ) : (
          // Refetches keep the previous render, dimmed, instead of flashing.
          <div className={cn("flex flex-col gap-5 transition-opacity", loading && "opacity-60")}>
            <AttentionStrip data={data} />
            <LatestWeek data={data} film={film} />
            <PerformanceTrend data={data} film={film} />
            <PositionGroups data={data} />
            <Games games={data.games} />
            <ReadinessCard readiness={data.readiness} />
          </div>
        )}
      </div>
    </>
  );
}

function Segmented({ label, options, value, onChange }: { label: string; options: { key: string; label: string }[]; value: string; onChange: (key: string) => void }) {
  return (
    <div role="radiogroup" aria-label={label} className="inline-flex rounded-md border border-border-soft p-0.5">
      {options.map((option) => (
        <button
          key={option.key}
          type="button"
          role="radio"
          aria-checked={value === option.key}
          onClick={() => onChange(option.key)}
          className={cn("min-h-8 rounded px-3 text-xs font-semibold transition-colors", value === option.key ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:text-foreground")}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}

function Section({ title, subtitle, action, children }: { title: string; subtitle?: string; action?: React.ReactNode; children: React.ReactNode }) {
  return (
    <Card>
      <CardHeader className="flex flex-row flex-wrap items-start justify-between gap-2">
        <div>
          <h2 className="font-display text-base font-semibold uppercase tracking-wide">{title}</h2>
          {subtitle && <p className="mt-0.5 text-xs text-muted-foreground">{subtitle}</p>}
        </div>
        {action}
      </CardHeader>
      <CardContent>{children}</CardContent>
    </Card>
  );
}

function AttentionStrip({ data }: { data: ApiTeamDashboard }) {
  const { activity } = data;
  const items = [
    { label: "Processing now", value: activity.film_processing, href: "/film-room/?tab=upload" },
    { label: "Waiting to process", value: activity.film_uploaded, href: "/film-room/?tab=upload" },
    { label: "Plays filmed this week", value: activity.plays_this_week, href: "/film-room/?tab=review" },
    { label: "Sessions this week", value: activity.sessions_this_week, href: "/film-room/?tab=browse" },
  ];
  return (
    <nav aria-label="Needs attention" className="grid grid-cols-2 gap-2 lg:grid-cols-4">
      {items.map((item) => (
        <Link key={item.label} href={item.href} className="flex items-center justify-between gap-2 rounded-lg border border-border-soft bg-card/60 px-3 py-2 text-xs hover:border-border">
          <span className="text-muted-foreground">{item.label}</span>
          <span className="text-base font-semibold text-foreground">{item.value}</span>
        </Link>
      ))}
    </nav>
  );
}

function weeksFor(data: ApiTeamDashboard, film: FilmFilter): ApiTeamWeek[] {
  return combineWeeks(film === "all" ? data.weekly : data.weekly.filter((w) => w.session_kind === film));
}

function LatestWeek({ data, film }: { data: ApiTeamDashboard; film: FilmFilter }) {
  const weeks = weeksFor(data, film);
  const latest = weeks[weeks.length - 1];
  const previous = weeks[weeks.length - 2];
  const games = [...data.games].filter((g) => g.offense).sort((a, b) => (a.date ?? "").localeCompare(b.date ?? ""));
  const lastGame = games[games.length - 1];
  const prevGame = games[games.length - 2];
  const versus = previous ? `vs week of ${shortDate(day(previous.week_start))}` : undefined;
  const trend = (key: keyof ApiTeamWeek) => weeks.slice(-12).map((w) => w[key] as number | null);

  if (!latest && !lastGame) {
    return (
      <EmptyState
        title="No measured film in this range yet"
        hint="Process practice or game film in Film Room. Speeds and distances appear here once players are tracked on a calibrated field."
      />
    );
  }
  return (
    <section aria-label="Latest week of film">
      <p className="mb-2 text-xs text-muted-foreground">
        {latest ? <>Latest film: week of <span className="text-foreground">{shortDate(day(latest.week_start))}</span></> : "No physical tracking in this range"}
      </p>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-5">
        <StatTile label="Reps measured" value={latest ? String(latest.reps) : "—"} change={change(latest?.reps, previous?.reps)} formatChange={(v) => String(Math.round(v))} changeLabel={versus} trend={trend("reps")} />
        <StatTile label="Team top speed (90th pct)" value={latest?.top_speed_p90_yps != null ? mph(latest.top_speed_p90_yps) : "—"} unit="mph" change={change(latest?.top_speed_p90_yps, previous?.top_speed_p90_yps)} formatChange={mphNum} changeLabel={versus} trend={trend("top_speed_p90_yps")} />
        <StatTile label="High-speed yards per rep" value={latest?.high_speed_yards_per_rep != null ? yd(latest.high_speed_yards_per_rep) : "—"} unit="yd" change={change(latest?.high_speed_yards_per_rep, previous?.high_speed_yards_per_rep)} formatChange={yd} changeLabel={versus} trend={trend("high_speed_yards_per_rep")} />
        <StatTile label="Sprints per rep" value={latest?.sprints_per_rep != null ? latest.sprints_per_rep.toFixed(2) : "—"} change={change(latest?.sprints_per_rep, previous?.sprints_per_rep)} formatChange={(v) => v.toFixed(2)} changeLabel={versus} trend={trend("sprints_per_rep")} />
        <StatTile
          label={lastGame ? `Offense yards per play${lastGame.opponent ? ` · ${lastGame.opponent}` : ""}` : "Offense yards per play"}
          value={lastGame?.offense?.yards_per_play != null ? yd(lastGame.offense.yards_per_play) : "—"}
          change={change(lastGame?.offense?.yards_per_play, prevGame?.offense?.yards_per_play)}
          formatChange={yd}
          changeLabel={prevGame ? "vs previous game" : undefined}
          hint={lastGame ? "Needs a second game to compare" : "No game linked to the play-by-play yet"}
          trend={games.map((g) => g.offense?.yards_per_play ?? null)}
        />
      </div>
    </section>
  );
}

function sessionSeries(data: ApiTeamDashboard, film: FilmFilter, key: keyof ApiTeamWeek, format: (v: number) => number = (v) => v): LineSeries[] {
  const kinds = film === "all" ? ["practice", "game"] : [film];
  return kinds
    .map((kind) => ({
      key: kind,
      label: SESSION_LABEL[kind],
      color: SESSION_COLORS[kind],
      points: data.weekly
        .filter((w) => w.session_kind === kind)
        .map((w) => ({ x: day(w.week_start), y: w[key] == null ? null : format(w[key] as number) })),
    }))
    .filter((s) => s.points.some((p) => p.y != null));
}

function PerformanceTrend({ data, film }: { data: ApiTeamDashboard; film: FilmFilter }) {
  const charts = [
    { title: "Top speed, 90th percentile", unit: "mph", key: "top_speed_p90_yps" as const, toUnit: (v: number) => v * 2.04545, fmt: (v: number) => v.toFixed(1) },
    { title: "High-speed yards per rep", unit: "yd", key: "high_speed_yards_per_rep" as const, toUnit: (v: number) => v, fmt: yd },
    { title: "Sprints per rep", unit: "", key: "sprints_per_rep" as const, toUnit: (v: number) => v, fmt: (v: number) => v.toFixed(2) },
    { title: "Yards per rep", unit: "yd", key: "yards_per_rep" as const, toUnit: (v: number) => v, fmt: yd },
  ];
  const any = charts.some((c) => sessionSeries(data, film, c.key).length);
  return (
    <Section title="Performance trend" subtitle="Weekly team rates from tracked reps. Practice and games keep their colours on every chart.">
      {!any ? (
        <p className="text-sm text-muted-foreground">No tracked reps in this range. Process film with a calibrated field to see weekly speed and distance.</p>
      ) : (
        <div className="grid gap-6 lg:grid-cols-2">
          {charts.map((chart) => {
            const series = sessionSeries(data, film, chart.key, chart.toUnit);
            return (
              <figure key={chart.key} className="min-w-0">
                <figcaption className="mb-1 text-sm font-medium">{chart.title}{chart.unit ? <span className="text-muted-foreground"> · {chart.unit}</span> : null}</figcaption>
                {series.length ? (
                  <LineChart series={series} ariaLabel={`${chart.title} by week`} formatY={chart.fmt} height={190} />
                ) : (
                  <p className="py-8 text-xs text-muted-foreground">Not measured in this range.</p>
                )}
              </figure>
            );
          })}
        </div>
      )}
    </Section>
  );
}

function PositionGroups({ data }: { data: ApiTeamDashboard }) {
  const groups = [...data.position_groups].sort((a, b) => {
    const ia = GROUP_ORDER.indexOf(a.position_group);
    const ib = GROUP_ORDER.indexOf(b.position_group);
    return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib) || a.position_group.localeCompare(b.position_group);
  });
  return (
    <Section
      title="Position groups"
      subtitle="Each group's latest week and change from its previous measured week."
      action={<Button asChild variant="ghost" size="sm"><Link href="/players/?tab=leaderboard">Leaderboards <ArrowRight className="size-3.5" /></Link></Button>}
    >
      {!groups.length ? (
        <p className="text-sm text-muted-foreground">Position groups appear once tracked players have a roster position or a detected role.</p>
      ) : (
        <ul className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
          {groups.map((group) => {
            const latest = group.weekly[group.weekly.length - 1];
            return (
              <li key={group.position_group} className="rounded-lg border border-border-soft p-3">
                <div className="flex items-center justify-between">
                  <p className="text-sm font-semibold">{group.position_group}</p>
                  <p className="text-[0.65rem] text-muted-foreground">{latest?.reps ?? 0} reps</p>
                </div>
                <div className="mt-2 flex items-end justify-between gap-2">
                  <div>
                    <p className="text-[0.65rem] text-muted-foreground">Top speed</p>
                    <p className="text-xl font-semibold">{latest?.top_speed_p90_yps != null ? mph(latest.top_speed_p90_yps) : "—"}<span className="ml-1 text-xs font-normal text-muted-foreground">mph</span></p>
                  </div>
                  <Sparkline values={group.weekly.map((w) => w.top_speed_p90_yps)} label={`${group.position_group} top speed by week`} />
                </div>
                <ChangeRow label="Top speed" value={group.change.top_speed_p90_yps} format={(v) => `${mph(v)} mph`} />
                <ChangeRow label="High-speed yd / rep" value={group.change.high_speed_yards_per_rep} format={(v) => `${yd(v)} yd`} />
              </li>
            );
          })}
        </ul>
      )}
    </Section>
  );
}

function ChangeRow({ label, value, format }: { label: string; value: number | null | undefined; format: (v: number) => string }) {
  return (
    <p className="mt-1 flex items-center justify-between text-xs">
      <span className="text-muted-foreground">{label}</span>
      {value == null ? (
        <span className="text-muted-foreground">One week so far</span>
      ) : (
        <span className={value > 0 ? "text-status-ok" : value < 0 ? "text-status-danger" : "text-muted-foreground"}>
          {value > 0 ? "▲ +" : value < 0 ? "▼ −" : ""}{value === 0 ? "No change" : format(Math.abs(value))}
        </span>
      )}
    </p>
  );
}

function Games({ games }: { games: ApiDashboardGame[] }) {
  const router = useRouter();
  const linked = useMemo(() => games.filter((g) => g.offense || g.defense).sort((a, b) => (a.date ?? "").localeCompare(b.date ?? "")), [games]);
  const latest = linked[linked.length - 1];
  const byX = new Map(linked.filter((g) => g.date).map((g) => [day(g.date as string), g.video_id]));
  const open = (_: string, x: number) => {
    const id = byX.get(x);
    if (id) router.push(`/team-stats/?videoId=${encodeURIComponent(id)}`);
  };
  const series = (pick: (side: ApiGameSideSummary) => number | null): LineSeries[] => [
    { key: "offense", label: "Offense", color: OFFENSE_COLOR, points: linked.filter((g) => g.date).map((g) => ({ x: day(g.date as string), y: g.offense ? pick(g.offense) : null })) },
    { key: "defense", label: "Defense (allowed)", color: DEFENSE_COLOR, points: linked.filter((g) => g.date).map((g) => ({ x: day(g.date as string), y: g.defense ? pick(g.defense) : null })) },
  ];
  return (
    <Section
      title="Games"
      subtitle="Toledo's offense and the yards its defense allowed, from film matched to the official play-by-play."
      action={<Button asChild variant="ghost" size="sm"><Link href="/team-stats/">Team Stats <ArrowRight className="size-3.5" /></Link></Button>}
    >
      {!latest ? (
        <p className="text-sm text-muted-foreground">No game film is linked to an official play-by-play in this range yet. Process a game and its plays link automatically.</p>
      ) : (
        <div className="flex flex-col gap-6">
          <LatestGame game={latest} />
          {linked.length > 1 ? (
            <div className="grid gap-6 lg:grid-cols-3">
              {[
                { title: "Yards per play", pick: (s: ApiGameSideSummary) => s.yards_per_play, fmt: yd },
                { title: "Success rate", pick: (s: ApiGameSideSummary) => s.success_rate, fmt: pct },
                { title: "EPA per play", pick: (s: ApiGameSideSummary) => s.epa_per_play, fmt: epa },
              ].map((chart) => (
                <figure key={chart.title} className="min-w-0">
                  <figcaption className="mb-1 text-sm font-medium">{chart.title} by game</figcaption>
                  <LineChart series={series(chart.pick)} ariaLabel={`${chart.title} by game`} formatY={chart.fmt} height={180} onPointClick={open} />
                </figure>
              ))}
            </div>
          ) : (
            <p className="text-xs text-muted-foreground">Season trend lines appear from the second linked game.</p>
          )}
        </div>
      )}
    </Section>
  );
}

function LatestGame({ game }: { game: ApiDashboardGame }) {
  const rows: { label: string; key: keyof ApiGameSideSummary; fmt: (v: number) => string }[] = [
    { label: "Yards per play", key: "yards_per_play", fmt: yd },
    { label: "Success rate", key: "success_rate", fmt: pct },
    { label: "3rd-down rate", key: "third_down_rate", fmt: pct },
    { label: "EPA per play", key: "epa_per_play", fmt: epa },
    { label: "Explosive plays", key: "explosive_plays", fmt: (v) => String(v) },
    { label: "Turnovers", key: "turnovers", fmt: (v) => String(v) },
  ];
  return (
    <div>
      <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
        <p className="text-sm">
          <span className="font-semibold">{game.opponent ? `vs ${game.opponent}` : "Latest game"}</span>
          {game.date && <span className="text-muted-foreground"> · {shortDate(day(game.date))}</span>}
          <span className="text-muted-foreground"> · {game.linked_plays} plays linked</span>
        </p>
        <Link href={`/team-stats/?videoId=${encodeURIComponent(game.video_id)}`} className="text-xs text-primary hover:underline">Full breakdown</Link>
      </div>
      <ul className="mb-2 flex gap-4 text-xs text-muted-foreground" aria-label="Legend">
        <li className="flex items-center gap-1.5"><span aria-hidden className="inline-block size-2.5 rounded-sm" style={{ background: OFFENSE_COLOR }} />Offense</li>
        <li className="flex items-center gap-1.5"><span aria-hidden className="inline-block size-2.5 rounded-sm" style={{ background: DEFENSE_COLOR }} />Defense (allowed)</li>
      </ul>
      <div className="grid gap-x-8 gap-y-3 sm:grid-cols-2 xl:grid-cols-3">
        {rows.map((row) => {
          const off = game.offense?.[row.key] as number | null | undefined;
          const def = game.defense?.[row.key] as number | null | undefined;
          // EPA can be negative: bars show magnitude, the value keeps its sign.
          const max = Math.max(Math.abs(off ?? 0), Math.abs(def ?? 0)) || 1;
          return (
            <div key={row.key} className="min-w-0">
              <p className="mb-1 text-xs text-muted-foreground">{row.label}</p>
              <PairBar color={OFFENSE_COLOR} value={off} max={max} fmt={row.fmt} label="Offense" />
              <PairBar color={DEFENSE_COLOR} value={def} max={max} fmt={row.fmt} label="Defense (allowed)" />
            </div>
          );
        })}
      </div>
    </div>
  );
}

function PairBar({ color, value, max, fmt, label }: { color: string; value: number | null | undefined; max: number; fmt: (v: number) => string; label: string }) {
  return (
    <div className="mb-[2px] grid grid-cols-[minmax(0,1fr)_3.5rem] items-center gap-2" title={`${label}: ${value != null ? fmt(value) : "—"}`}>
      <span className="relative h-3">
        {value != null && <span className="absolute inset-y-0 left-0 rounded-r" style={{ width: `${(Math.abs(value) / max) * 100}%`, background: color }} />}
      </span>
      <span className="text-right text-xs font-semibold" style={{ fontVariantNumeric: "tabular-nums" }}>{value != null ? fmt(value) : "—"}</span>
    </div>
  );
}

function ReadinessCard({ readiness }: { readiness: ApiTeamDashboard["readiness"] }) {
  if (!readiness.visible) {
    return (
      <Section title="Readiness">
        <p className="flex items-center gap-2 text-sm text-muted-foreground"><HeartPulse className="size-4" /> Workload and injury-risk data is visible to sports performance and analyst staff.</p>
      </Section>
    );
  }
  const groups = readiness.groups.filter((g) => g.mean_injury_risk_score != null);
  return (
    <Section
      title="Readiness"
      subtitle={readiness.as_of ? `Workload risk by position group, 7 days to ${shortDate(day(readiness.as_of))}. A screening signal for staff, not a diagnosis.` : undefined}
      action={<Button asChild variant="ghost" size="sm"><Link href="/health-workload/">Health &amp; workload <ArrowRight className="size-3.5" /></Link></Button>}
    >
      {!groups.length ? (
        <p className="text-sm text-muted-foreground">No workload history yet. It builds from tracked practice and game reps for identified players.</p>
      ) : (
        <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
          <BarList
            ariaLabel="Mean workload risk by position group"
            color={SERIES_COLORS[0]}
            max={1}
            format={(v) => v.toFixed(2)}
            threshold={{ value: RISK_FLAG, label: `Flag at ${RISK_FLAG}` }}
            rows={groups.map((g) => ({
              key: g.position_group,
              label: g.position_group,
              value: g.mean_injury_risk_score,
              note:
                g.max_injury_risk_score != null && g.max_injury_risk_score >= RISK_FLAG ? (
                  <span className="flex items-center gap-1 font-normal text-status-warn"><AlertTriangle aria-hidden className="size-3.5" />has flagged player</span>
                ) : undefined,
            }))}
          />
          {readiness.player_level ? (
            <div>
              <p className="mb-2 text-xs font-medium">Flagged players</p>
              {readiness.flagged.length ? (
                <ul className="flex flex-col gap-1.5 text-xs">
                  {readiness.flagged.map((p) => (
                    <li key={p.player_id} className="flex items-center justify-between gap-2 rounded-md border border-border-soft px-2.5 py-1.5">
                      <Link href="/health-workload/" className="hover:underline">
                        {p.jersey_number != null ? `#${p.jersey_number} ` : ""}{p.name}
                        {p.position_group ? <span className="text-muted-foreground"> · {p.position_group}</span> : null}
                      </Link>
                      <span className="flex items-center gap-1 text-status-warn"><AlertTriangle aria-hidden className="size-3.5" />{p.injury_risk_score.toFixed(2)}{p.acwr != null ? <span className="text-muted-foreground"> · ACWR {p.acwr.toFixed(2)}</span> : null}</span>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="text-xs text-muted-foreground">No player is at or above {RISK_FLAG}.</p>
              )}
            </div>
          ) : (
            <p className="text-xs text-muted-foreground">Player-level flags are visible to sports performance staff.</p>
          )}
        </div>
      )}
    </Section>
  );
}
