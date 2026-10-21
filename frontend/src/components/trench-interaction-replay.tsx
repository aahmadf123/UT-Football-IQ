"use client";

import { useMemo, useState } from "react";
import type { FilmEvidence } from "@/lib/analytics-api";
import { canonicalShotId } from "@/lib/shot-id";
import type { Interaction } from "@/components/interaction-position-visuals";

type Position = { x: number; y: number };
type Sample = { frame: number; seconds: number; blocker: Position | null; defender: Position | null };
type Replay = { interaction: Interaction; samples: Sample[] };
const object = (value: unknown): Record<string, unknown> | null => value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null;
const finite = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value);
function position(value: unknown): Position | null {
  const point = object(value);
  return point && finite(point.field_x) && finite(point.field_y) && point.field_x >= -10 && point.field_x <= 110 && Math.abs(point.field_y) <= 80 / 3
    ? { x: point.field_x, y: point.field_y } : null;
}

/** Geometry must belong to this paired measurement and its exact native clocks. */
export function qualifiedTrenchReplays(observations: Interaction[]): Replay[] {
  return observations.flatMap((interaction) => {
    const packet = object(interaction.row.metric_packet?.interaction_geometry);
    if (!interaction.row.video_id || !interaction.row.clip_id || !interaction.row.result_version
      || !packet || packet.schema_version !== 1 || packet.coordinate_space !== "field_yards"
      || packet.clock_basis !== "verified_native_pts" || packet.calibration_safe !== true || packet.series_interpolation !== "none"
      || canonicalShotId(packet.shot_id) !== interaction.shot || packet.blocker_id !== interaction.blocker || packet.defender_id !== interaction.defender
      || packet.source_start_seconds !== interaction.seconds || packet.source_end_seconds !== interaction.row.metric_packet?.source_end_seconds
      || !Array.isArray(packet.samples)) return [];
    const samples: Sample[] = [];
    for (const raw of packet.samples) {
      const sample = object(raw);
      const previous = samples.at(-1);
      if (!sample || !Number.isInteger(sample.frame_number) || (sample.frame_number as number) < 0 || !finite(sample.timestamp_seconds)
        || sample.timestamp_seconds < interaction.seconds || sample.timestamp_seconds > (packet.source_end_seconds as number)
        || previous && (sample.timestamp_seconds <= previous.seconds || (sample.frame_number as number) <= previous.frame)) return [];
      const blocker = position(sample.blocker), defender = position(sample.defender);
      // Malformed coordinates cannot masquerade as explicitly absent samples.
      if (sample.blocker !== null && !blocker || sample.defender !== null && !defender) return [];
      samples.push({ frame: sample.frame_number as number, seconds: sample.timestamp_seconds, blocker, defender });
    }
    return samples.length >= 2 && samples.some((sample) => sample.blocker && sample.defender) ? [{ interaction, samples }] : [];
  });
}

function traces(samples: Sample[], actor: "blocker" | "defender"): Position[][] {
  const segments: Position[][] = [];
  let current: Position[] = [];
  let previous: Sample | null = null;
  for (const sample of samples) {
    if (!sample[actor] || previous && sample.seconds - previous.seconds > .2 + 1e-9) {
      if (current.length) segments.push(current);
      current = [];
    }
    if (sample[actor]) current.push(sample[actor]);
    previous = sample;
  }
  if (current.length) segments.push(current);
  return segments;
}

export function TrenchInteractionReplay({ observations, onSelect }: { observations: Interaction[]; onSelect: (row: FilmEvidence) => void }) {
  const replays = useMemo(() => qualifiedTrenchReplays(observations), [observations]);
  const [selected, setSelected] = useState("");
  const [index, setIndex] = useState(0);
  const active = replays.find((replay) => replay.interaction.row.metric_id === selected) ?? replays[0];
  const currentIndex = active ? Math.min(index, active.samples.length - 1) : 0;
  const sample = active?.samples[currentIndex];
  if (!active || !sample) return null;
  const positions = active.samples.flatMap((item) => [item.blocker, item.defender].filter((point): point is Position => point !== null));
  const minX = Math.min(...positions.map((point) => point.x)) - 2, maxX = Math.max(...positions.map((point) => point.x)) + 2;
  const minY = Math.min(...positions.map((point) => point.y)) - 2, maxY = Math.max(...positions.map((point) => point.y)) + 2;
  const actors = ["blocker", "defender"] as const;
  const gap = currentIndex > 0 && sample.seconds - active.samples[currentIndex - 1].seconds > .2 + 1e-9;
  return <section aria-label="Observed trench interaction replay" className="mt-5 border-t border-border-soft pt-4">
    <h3 className="text-sm font-semibold">Trench frame replay</h3>
    <p className="mt-1 text-xs text-muted-foreground">Exact observed blocker and defender positions in field yards. Missing actors stay absent; paths stop at missing observations and gaps above 200 ms. Interaction endpoints come from separate evidence. Positions do not grade blocking success or assignments.</p>
    <label className="mt-3 flex flex-col gap-1 text-xs">Measured interaction<select className="rounded border border-border-soft bg-background p-2" value={active.interaction.row.metric_id} onChange={(event) => { setSelected(event.target.value); setIndex(0); }}>{replays.map((replay, i) => <option key={replay.interaction.row.metric_id} value={replay.interaction.row.metric_id}>Pair {i + 1} · {replay.interaction.seconds.toFixed(2)}–{(replay.interaction.row.metric_packet?.source_end_seconds as number).toFixed(2)}s</option>)}</select></label>
    <label className="mt-3 flex flex-col gap-1 text-xs">Native frame {sample.frame} · {sample.seconds.toFixed(3)}s<input aria-label="Interaction source frame" type="range" min={0} max={active.samples.length - 1} step={1} value={currentIndex} onChange={(event) => setIndex(Number(event.target.value))} /></label>
    <div className="mt-2 flex gap-3 text-xs"><button type="button" className="text-primary underline disabled:opacity-40" disabled={currentIndex === 0} onClick={() => setIndex(currentIndex - 1)}>Previous observed frame</button><button type="button" className="text-primary underline disabled:opacity-40" disabled={currentIndex === active.samples.length - 1} onClick={() => setIndex(currentIndex + 1)}>Next observed frame</button></div>
    {gap ? <p role="status" className="mt-2 text-xs text-muted-foreground">Observation gap before this frame; no motion is drawn across it.</p> : null}
    <svg role="img" aria-label={`Paired trench positions at native frame ${sample.frame}`} viewBox={`${minX} ${minY} ${maxX - minX} ${maxY - minY}`} className="mt-3 h-64 w-full rounded bg-primary/5">
      {actors.map((actor, i) => <g key={actor} fill={i ? "#38bdf8" : "#e4bb50"} stroke={i ? "#38bdf8" : "#e4bb50"}>
        {traces(active.samples.slice(0, currentIndex + 1), actor).map((segment, part) => <polyline key={part} points={segment.map((point) => `${point.x},${point.y}`).join(" ")} fill="none" strokeWidth=".06" />)}
        {sample[actor] ? <><circle cx={sample[actor].x} cy={sample[actor].y} r=".2" /><text x={sample[actor].x + .35} y={sample[actor].y + .15} fontSize=".45" stroke="none">{actor === "blocker" ? "B" : "D"}</text></> : null}
      </g>)}
    </svg>
    <table className="mt-2 w-full text-left text-xs tabular-nums"><caption className="sr-only">Exact paired source positions in field yards</caption><thead><tr><th className="p-2">Actor</th><th className="p-2">Field x/y (yd)</th><th className="p-2">Track</th></tr></thead><tbody>{actors.map((actor) => <tr key={actor}><th className="p-2 capitalize">{actor}</th><td className="p-2">{sample[actor] ? `${sample[actor].x.toFixed(2)} / ${sample[actor].y.toFixed(2)}` : "Unobserved"}</td><td className="break-all p-2">{active.interaction[actor]}</td></tr>)}</tbody></table>
    <button type="button" className="mt-2 text-sm text-primary underline" onClick={() => onSelect({ ...active.interaction.row, focus_seconds: sample.seconds })}>Inspect paired film at {sample.seconds.toFixed(3)}s</button>
    <p className="mt-2 text-xs text-muted-foreground">Shot {active.interaction.shot} · definition {active.interaction.definition} · {active.samples.length} source samples.</p>
  </section>;
}
