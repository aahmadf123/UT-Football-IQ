"use client";

import { useMemo } from "react";
import { EChart } from "@/components/charts/echart";
import type { CensoringGroup, MetricEvidence } from "@/lib/analytics-api";
export type { CensoringGroup, SurvivalStep } from "@/lib/analytics-api";
const finite = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value);
const count = (value: unknown): value is number => finite(value) && Number.isInteger(value) && value >= 0;
const CENSORED_DURATIONS = new Set(["ball_holding_duration", "time_to_first_pressure", "tackle_box_residence"]);

/** Validate supplied qualified observations and curve; never estimate a client curve. */
export function suppliedCensoringGroups(groups?: CensoringGroup[]): CensoringGroup[] {
  return (groups ?? []).filter((group) => {
    if (!CENSORED_DURATIONS.has(group.metric_name) || !group.definition_version || group.unit !== "s" || !count(group.n) || !count(group.events)
      || !count(group.censored) || group.n < 1 || group.censored < 1 || group.events + group.censored !== group.n
      || !Array.isArray(group.evidence) || group.evidence.length !== group.n || !Array.isArray(group.survival_curve)
      || group.survival_curve.length < 2) return false;
    if (group.evidence.some((row) => !row.clip_id || !row.play_id || !row.metric_id || !finite(row.value) || row.value < 0 || typeof row.right_censored !== "boolean")
      || group.evidence.filter((row) => row.right_censored).length !== group.censored) return false;
    let seconds = -1, survival = 1, remaining = group.n, eventTotal = 0, censorTotal = 0;
    for (const step of group.survival_curve) {
      if (!finite(step.seconds) || step.seconds < 0 || step.seconds < seconds || !finite(step.survival)
        || step.survival < 0 || step.survival > survival || !count(step.at_risk) || !count(step.events)
        || !count(step.censored) || step.events + step.censored > step.at_risk || step.at_risk !== remaining) return false;
      seconds = step.seconds; survival = step.survival;
      remaining -= step.events + step.censored;
      eventTotal += step.events; censorTotal += step.censored;
    }
    const initial = group.survival_curve[0];
    return initial.seconds === 0 && initial.survival === 1 && initial.at_risk === group.n
      && initial.events === 0 && initial.censored === 0 && remaining === 0
      && eventTotal === group.events && censorTotal === group.censored;
  });
}

export function CensoringVisuals({ groups, onSelect }: { groups?: CensoringGroup[]; onSelect: (row: MetricEvidence) => void }) {
  const supplied = useMemo(() => suppliedCensoringGroups(groups), [groups]);
  if (!supplied.length) return null;
  return <section aria-label="Censored duration evidence" className="grid gap-6 xl:grid-cols-2">{supplied.map((group) => <SurvivalGroup key={`${group.key}-${group.metric_name}-${group.definition_version}`} group={group} onSelect={onSelect} />)}</section>;
}

function SurvivalGroup({ group, onSelect }: { group: CensoringGroup; onSelect: (row: MetricEvidence) => void }) {
  const label = group.metric_name.replaceAll("_", " ");
  const option = useMemo(() => ({
    animation: false, grid: { left: 58, right: 24, bottom: 48, top: 24 },
    xAxis: { type: "value", name: "Duration (s)", min: 0, nameLocation: "middle", nameGap: 28 },
    yAxis: { type: "value", name: "Survival probability", min: 0, max: 1 }, tooltip: { trigger: "item" },
    series: [{ type: "line", step: "end", showSymbol: true, symbolSize: 6, connectNulls: false,
      data: group.survival_curve.map((step) => [step.seconds, step.survival]) }],
  }), [group]);
  return <section aria-label={`${label} censoring`} className="min-w-0 border-t border-border-soft pt-4">
    <h2 className="text-base font-semibold capitalize">{label} · observed endpoints and lower bounds</h2>
    <p className="mt-1 text-xs text-muted-foreground">n={group.n} qualified duration observations · {group.events} observed endpoint{group.events === 1 ? "" : "s"} · {group.censored} right-censored · seconds · Definition {group.definition_version}.</p>
    <p className="mt-2 text-xs text-muted-foreground">A censored duration is a lower bound: the endpoint was not observed before its observation window ended. These values stay outside ordinary completed-duration averages. This survival curve is supplied by the qualified analysis.</p>
    <EChart option={option} label={`${label} survival, ${group.n} observations with ${group.censored} censored. Accessible risk sets and film evidence follow.`} onSelect={(index) => {
      const step = group.survival_curve[index];
      if (!step) return;
      const row = [...group.evidence].sort((left, right) => Math.abs(left.value - step.seconds) - Math.abs(right.value - step.seconds))[0];
      if (row) onSelect(row);
    }} />
    <details><summary className="cursor-pointer text-sm">Survival steps and risk sets</summary><div className="max-h-64 overflow-auto"><table className="w-full text-left text-xs tabular-nums"><caption className="sr-only">Supplied survival curve for {label}</caption><thead><tr><th className="p-2">Duration (s)</th><th className="p-2">At risk</th><th className="p-2">Endpoints</th><th className="p-2">Censored</th><th className="p-2">Survival</th></tr></thead><tbody>{group.survival_curve.map((step, index) => <tr key={index}><td className="p-2">{step.seconds.toFixed(2)}</td><td className="p-2">{step.at_risk}</td><td className="p-2">{step.events}</td><td className="p-2">{step.censored}</td><td className="p-2">{step.survival.toFixed(3)}</td></tr>)}</tbody></table></div></details>
    <details className="mt-3"><summary className="cursor-pointer text-sm">Duration observations and film evidence</summary><div className="max-h-64 overflow-auto"><table className="w-full text-left text-xs tabular-nums"><caption className="sr-only">Qualified duration observations for {label}</caption><thead><tr><th className="p-2">Duration (s)</th><th className="p-2">Observation</th><th className="p-2">Evidence</th></tr></thead><tbody>{group.evidence.map((row) => <tr key={row.metric_id}><td className="p-2">{row.right_censored ? "≥ " : ""}{row.value.toFixed(2)}</td><td className="p-2">{row.right_censored ? "Right-censored lower bound" : "Observed endpoint"}</td><td className="p-2"><button type="button" className="text-primary underline" onClick={() => onSelect(row)}>Inspect duration</button></td></tr>)}</tbody></table></div></details>
  </section>;
}
