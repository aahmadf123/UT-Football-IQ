"use client";

import { useMemo } from "react";
import type { AnalyticsGroup, FilmEvidence, MetricEvidence } from "@/lib/analytics-api";
import { canonicalShotId } from "@/lib/shot-id";

const finite = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value);
const object = (value: unknown): Record<string, unknown> | null => value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null;
type Interaction = { row: MetricEvidence; blocker: string; defender: string; seconds: number; duration: number; definition: string; interactionId: string };

/** Explicit endpoint evidence supplies interactions; distance supplies none. */
export function qualifiedInteractions(groups: AnalyticsGroup[]): Interaction[] {
  const seen = new Set<string>();
  return groups.filter((group) => group.metric_name === "engagement_duration" && group.unit === "s" && group.definition_version)
    .flatMap((group) => group.evidence.flatMap((row) => {
      const packet = row.metric_packet;
      if (!row.tracklet_id || !row.result_version || row.right_censored || !packet || packet.track_id !== row.tracklet_id
        || typeof packet.defender_id !== "string" || !packet.defender_id || packet.defender_id === row.tracklet_id
        || typeof packet.interaction_id !== "string" || !packet.interaction_id || !finite(packet.source_start_seconds) || !finite(packet.source_end_seconds)
        || packet.source_start_seconds < row.start_seconds || packet.source_end_seconds > row.end_seconds || packet.source_end_seconds < packet.source_start_seconds
        || !finite(row.value) || Math.abs(row.value - (packet.source_end_seconds - packet.source_start_seconds)) > .002 || !Array.isArray(packet.event_evidence)) return [];
      const events = packet.event_evidence.flatMap<Record<string, unknown>>((raw) => {
        const event = object(raw);
        const shot = canonicalShotId(event?.shot_id);
        return event && typeof event.event_id === "string" && !!event.event_id
          && ["human", "manual", "real", "model"].includes(String(event.source)) && shot !== null
          ? [{ ...event, shot_id: shot }] : [];
      });
      const starts = events.filter((event) => event.event_type === "engagement_start" && event.timestamp_seconds === packet.source_start_seconds);
      const ends = events.filter((event) => event.event_type === "engagement_end" && event.timestamp_seconds === packet.source_end_seconds);
      if (starts.length !== 1 || ends.length !== 1 || starts[0].shot_id !== ends[0].shot_id) return [];
      const key = JSON.stringify([row.play_id, row.clip_id, row.tracklet_id, packet.defender_id, packet.interaction_id, row.result_version, group.definition_version]);
      if (seen.has(key)) return [];
      seen.add(key);
      return [{ row, blocker: row.tracklet_id, defender: packet.defender_id, seconds: packet.source_start_seconds,
        duration: row.value, definition: group.definition_version!, interactionId: packet.interaction_id }];
    }));
}

export function InteractionPositionVisuals({ groups, position = "", onSelect }: {
  groups: AnalyticsGroup[]; position?: string; onSelect: (row: FilmEvidence) => void;
}) {
  const observations = useMemo(() => !position || ["OL", "TE", "RB", "DL", "EDGE", "LB"].includes(position) ? qualifiedInteractions(groups) : [], [groups, position]);
  const plays = useMemo(() => [...new Set(observations.map((item) => item.row.play_id))], [observations]);
  if (!observations.length) return null;
  return <section aria-label="Blocker and defender interaction matrices" className="border-t border-border-soft pt-4"><h2 className="text-base font-semibold">Observed blocking interactions</h2><p className="mt-1 text-xs text-muted-foreground">{observations.length} interaction observations across {plays.length} canonical plays. Each cell preserves explicit start and end events for the same blocker–defender pair. Missing cells are unobserved. Duration does not establish blocking success or assignment correctness.</p><div className="mt-4 grid gap-4 xl:grid-cols-2">{plays.map((play, index) => {
    const rows = observations.filter((item) => item.row.play_id === play);
    const blockers = [...new Set(rows.map((item) => item.blocker))];
    const defenders = [...new Set(rows.map((item) => item.defender))];
    return <section key={play} className="min-w-0 overflow-x-auto"><h3 className="mb-2 text-sm font-medium">Contributing play {index + 1}</h3><table className="w-full text-left text-xs tabular-nums"><caption className="sr-only">Observed interaction endpoints for contributing play {index + 1}</caption><thead><tr><th className="p-2">Observed pair</th>{defenders.map((id, i) => <th className="p-2" key={id}>Defender {i + 1}</th>)}</tr></thead><tbody>{blockers.map((blocker, b) => <tr key={blocker} className="border-t border-border-soft"><th className="p-2">Blocker {b + 1}</th>{defenders.map((defender) => { const matched = rows.filter((item) => item.blocker === blocker && item.defender === defender); return <td className="p-2 align-top" key={defender}>{matched.length ? <div className="flex flex-col gap-2">{matched.map((item) => <div key={item.row.metric_id} className="rounded border border-primary/25 bg-primary/5 p-2"><span className="block">{item.duration.toFixed(2)} s</span><button type="button" className="mt-1 text-primary underline" onClick={() => onSelect({ ...item.row, focus_seconds: item.seconds })}>Inspect interaction at {item.seconds.toFixed(2)}s</button><details className="mt-1 text-muted-foreground"><summary className="cursor-pointer">Source and definition</summary><p className="mt-1 break-all">Definition {item.definition} · interaction {item.interactionId} · blocker track {item.blocker} · defender track {item.defender}</p></details></div>)}</div> : <span className="text-muted-foreground">Unobserved</span>}</td>; })}</tr>)}</tbody></table></section>;
  })}</div></section>;
}
