"use client";

import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useEffect, useMemo, useState } from "react";
import { useAppState } from "@/lib/app-state";
import { ANALYSIS_DEFAULTS, filmReviewHref, playerProfileContextHref, resolveAnalysisContext, updateContextParams, type AnalysisContext } from "@/lib/analysis-context";
import { deleteAnalysisView, fetchMetricDefinitions, fetchPlayEvidence, fetchSavedViews, queryAnalytics, saveAnalysisView, type AnalyticsGroup, type AnalyticsResult, type MetricDefinition, type MetricEvidence, type PlayEvidence, type SavedAnalysisView } from "@/lib/analytics-api";
import { friendlyMessage } from "@/lib/api-error";
import { EChart } from "@/components/charts/echart";
import { GameSummaryView } from "@/components/game-summary-view";
import { LearningPanel } from "@/components/learning-panel";
import { QualifiedPositionVisuals } from "@/components/qualified-position-visuals";
import { CensoringVisuals, suppliedCensoringGroups } from "@/components/censoring-visuals";
import { AnalysisCohortFilters } from "@/components/analysis-cohort-filters";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { NativeSelect } from "@/components/ui/native-select";

const POSITIONS = ["QB", "RB", "WR", "TE", "OL", "DL", "EDGE", "LB", "DB", "ST"];
const fieldClass = "flex min-w-32 flex-1 flex-col gap-1.5 text-xs text-muted-foreground";

export function TeamAnalytics() {
  const { authToken, selectedDate, sessionType, data, currentRole } = useAppState();
  const params = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();
  const [views, setViews] = useState<SavedAnalysisView[]>([]);
  const [definitions, setDefinitions] = useState<MetricDefinition[]>([]);
  const [definitionError, setDefinitionError] = useState("");
  const [result, setResult] = useState<AnalyticsResult | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [viewError, setViewError] = useState("");
  const [name, setName] = useState("");
  const [visibility, setVisibility] = useState<"private" | "team">("private");
  const [saving, setSaving] = useState(false);
  const [revision, setRevision] = useState(0);
  const [selected, setSelected] = useState<MetricEvidence | null>(null);
  const [evidence, setEvidence] = useState<PlayEvidence | null>(null);
  const [evidenceError, setEvidenceError] = useState("");
  const savedId = params.get("view") ?? "";
  const saved = views.find((view) => view.id === savedId);
  const context = resolveAnalysisContext(new URLSearchParams(params.toString()), saved?.filters, {
    since: selectedDate ?? "", until: selectedDate ?? "", session_kind: sessionType === "all" ? "" : sessionType,
  });
  const contextKey = JSON.stringify(context);
  const tab = params.get("tab") ?? "observed";
  const update = (patch: Partial<AnalysisContext>, extra: Record<string, string> = {}) => {
    // Materialize current resolved defaults so switching views obeys URL precedence.
    const next = updateContextParams(new URLSearchParams(params.toString()), { ...context, ...patch });
    for (const [key, value] of Object.entries(extra)) next.set(key, value);
    router.replace(`${pathname}?${next}`, { scroll: false });
  };

  useEffect(() => {
    let cancelled = false;
    fetchSavedViews(authToken).then((items) => { if (!cancelled) { setViews(items); setViewError(""); } }).catch((cause) => { if (!cancelled) setViewError(friendlyMessage(cause)); });
    fetchMetricDefinitions(authToken).then((payload) => { if (!cancelled) { setDefinitions(payload.definitions); setDefinitionError(""); } }).catch((cause) => { if (!cancelled) setDefinitionError(friendlyMessage(cause)); });
    return () => { cancelled = true; };
  }, [authToken, revision]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true); setError(""); setSelected(null); setEvidence(null);
    queryAnalytics(JSON.parse(contextKey) as AnalysisContext, authToken)
      .then((payload) => { if (!cancelled) setResult(payload); })
      .catch((cause) => { if (!cancelled) { setResult(null); setError(friendlyMessage(cause)); } })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [contextKey, authToken, revision]);

  useEffect(() => {
    if (!selected) return;
    let cancelled = false;
    setEvidence(null); setEvidenceError("");
    fetchPlayEvidence(selected.play_id, authToken).then((payload) => { if (!cancelled) setEvidence(payload); }).catch((cause) => { if (!cancelled) setEvidenceError(friendlyMessage(cause)); });
    return () => { cancelled = true; };
  }, [selected, authToken]);

  async function save(event: React.FormEvent) {
    event.preventDefault(); setSaving(true); setViewError("");
    try {
      const view = await saveAnalysisView({ name: name.trim(), surface: "analytics", filters: context, visibility }, authToken);
      setViews((current) => [...current, view]); setName(""); update({}, { view: view.id });
    } catch (cause) { setViewError(friendlyMessage(cause)); }
    finally { setSaving(false); }
  }
  async function removeView() {
    if (!saved) return;
    setSaving(true); setViewError("");
    try { await deleteAnalysisView(saved.id, authToken); setViews((current) => current.filter((view) => view.id !== saved.id)); update({}, { view: "" }); }
    catch (cause) { setViewError(friendlyMessage(cause)); }
    finally { setSaving(false); }
  }
  const filmHref = (row: MetricEvidence, rows: MetricEvidence[]) => filmReviewHref(new URLSearchParams(params.toString()), row.clip_id, {
    playlist: [...new Set(rows.map((item) => item.clip_id))].join(","), from: `${pathname}?${params}`, t: "0",
  });

  return <div className="flex flex-col gap-6" data-testid="team-analytics">
    <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border-soft pb-3">
      <nav aria-label="Analysis workspace" className="flex flex-wrap gap-2">
        {[ ["observed", "Practice & game observations"], ["official", "Game outcomes"], ["analyst", "Analyst tools"] ].map(([key, label]) => <Button key={key} variant={tab === key ? "default" : "ghost"} size="sm" onClick={() => update({}, { tab: key })} aria-pressed={tab === key}>{label}</Button>)}
      </nav>
      <div role="group" aria-label="Workspace layout" className="flex gap-1">{(["coach", "analyst"] as const).map((layout) => <Button key={layout} size="sm" variant={context.layout === layout ? "secondary" : "ghost"} aria-pressed={context.layout === layout} onClick={() => update({ layout })}>{layout === "coach" ? "Coach" : "Analyst"}</Button>)}</div>
    </div>
    {tab === "official" ? <GameSummaryView /> : <>
      <section aria-label="Analysis filters" className="flex flex-wrap gap-3">
        <label className={fieldClass}>Season<Input type="number" min="1900" max="2100" placeholder="All seasons" value={context.season} onChange={(event) => update({ season: event.target.value })} /></label>
        <label className={fieldClass}>From<Input type="date" value={context.since} onChange={(event) => update({ since: event.target.value })} /></label>
        <label className={fieldClass}>Through<Input type="date" value={context.until} min={context.since} onChange={(event) => update({ until: event.target.value })} /></label>
        <label className={fieldClass}>Session<NativeSelect value={context.session_kind} onChange={(event) => update({ session_kind: event.target.value })}><option value="">Practice & games</option><option value="practice">Practice</option><option value="game">Game</option><option value="scrimmage">Scrimmage</option></NativeSelect></label>
        <label className={fieldClass}>Library<NativeSelect value={context.library_role} onChange={(event) => update({ library_role: event.target.value })}><option value="">All film</option><option value="toledo">Toledo</option><option value="opponent">Opponent</option></NativeSelect></label>
        <label className={fieldClass}>Unit<NativeSelect value={context.side_of_ball} onChange={(event) => update({ side_of_ball: event.target.value })}><option value="">All units</option><option value="offense">Offense</option><option value="defense">Defense</option><option value="special_teams">Special teams</option></NativeSelect></label>
        <label className={fieldClass}>Player<NativeSelect value={context.player_id} onChange={(event) => update({ player_id: event.target.value })}><option value="">All players</option>{data.players.map((player) => <option key={player.id} value={player.id}>{player.name}</option>)}</NativeSelect></label>
        <label className={fieldClass}>Measurement<NativeSelect value={context.metric} onChange={(event) => update({ metric: event.target.value })}><option value="">All qualified measurements</option>{definitions.map((definition) => <option key={definition.name} value={definition.name}>{definition.label}</option>)}</NativeSelect></label>
        {context.layout === "analyst" ? <label className={fieldClass}>Compare<NativeSelect value={context.group_by} onChange={(event) => update({ group_by: event.target.value as AnalysisContext["group_by"] })}><option value="metric">Measurements</option><option value="player">Players</option><option value="position">Positions</option><option value="session_kind">Practice & game</option></NativeSelect></label> : null}
        <Button size="sm" variant="ghost" className="self-end" onClick={() => update({ ...ANALYSIS_DEFAULTS }, { view: "" })}>Clear filters</Button>
      </section>
      {context.player_id ? <Button asChild size="sm" variant="outline" className="self-start"><Link href={playerProfileContextHref(updateContextParams(new URLSearchParams(params.toString()), context), context.player_id, `${pathname}?${params}`)}>Open player profile</Link></Button> : null}
      {definitionError ? <p role="alert" className="text-sm text-status-danger">Measurement definitions could not load. {definitionError}<Button variant="link" size="sm" onClick={() => setRevision((value) => value + 1)}>Retry definitions</Button></p> : null}
      <AnalysisCohortFilters context={context} onChange={update} token={authToken} />
      <div role="group" aria-label="Position" className="flex flex-wrap gap-1 border-b border-border-soft pb-3">{["", ...POSITIONS].map((position) => <Button key={position} size="sm" variant={context.position === position ? "default" : "ghost"} aria-pressed={context.position === position} onClick={() => update({ position })}>{position || "All positions"}</Button>)}</div>
      <details className="border-b border-border-soft pb-4"><summary className="cursor-pointer text-sm font-medium">Saved views{saved ? ` · ${saved.name}` : ""}</summary>
        <div className="mt-3 flex flex-wrap items-end gap-3"><label className={fieldClass}>Open view<NativeSelect value={savedId} onChange={(event) => { const view = views.find((item) => item.id === event.target.value); update({ ...ANALYSIS_DEFAULTS, ...view?.filters }, { view: view?.id ?? "" }); }}><option value="">Choose saved view</option>{views.filter((view) => view.surface === "analytics").map((view) => <option key={view.id} value={view.id}>{view.name} · {view.visibility}</option>)}</NativeSelect></label>{saved?.can_edit ? <Button size="sm" variant="outline" disabled={saving} onClick={removeView}>Delete selected view</Button> : null}</div>
        <form onSubmit={save} className="mt-3 flex flex-wrap items-end gap-3"><label className={fieldClass}>View name<Input value={name} maxLength={120} required onChange={(event) => setName(event.target.value)} placeholder="Tuesday practice · receivers" /></label><label className={fieldClass}>Visibility<NativeSelect value={visibility} onChange={(event) => setVisibility(event.target.value as "private" | "team")}><option value="private">Only me</option><option value="team">Team staff</option></NativeSelect></label><Button size="sm" disabled={saving || !name.trim()}>{saving ? "Saving…" : "Save current view"}</Button></form>
        {viewError ? <p role="alert" className="mt-2 text-sm text-status-danger">{viewError} <Button variant="link" size="sm" onClick={() => setRevision((value) => value + 1)}>Retry</Button></p> : null}
      </details>
      {loading ? <p role="status" className="py-8 text-sm text-muted-foreground">Loading the selected cohort…</p> : error ? <div role="alert" className="py-6 text-sm"><p>{error}</p><Button className="mt-3" variant="outline" onClick={() => setRevision((value) => value + 1)}>Retry analysis</Button></div> : result ? <>
        <p className="text-sm text-muted-foreground" data-testid="cohort-denominator"><strong className="text-foreground">{result.cohort.plays}</strong> canonical plays · {result.cohort.player_reps} player reps · {result.cohort.eligible_measurements} eligible measurements · {result.cohort.excluded_measurements} excluded · {result.cohort.unknown_count ?? 0} unknown</p>
        <QualifiedPositionVisuals groups={result.groups} position={context.position} onSelect={setSelected} />
        <CensoringVisuals groups={result.censoring} onSelect={setSelected} />
        {!result.groups.length && !suppliedCensoringGroups(result.censoring).length ? <div className="border-y border-border-soft py-10"><h2 className="text-lg font-semibold">No qualified measurements in this cohort</h2><p className="mt-2 max-w-2xl text-sm text-muted-foreground">Film remains available while analysis runs. Change the filters or inspect processing status in Film Room. Unknown identity, missing calibration, or unqualified models do not become zero-valued statistics.</p><Button asChild className="mt-4" variant="outline"><Link href="/film-room/">Open Film Room</Link></Button></div> : <div className="grid items-start gap-6 xl:grid-cols-2">{result.groups.map((group) => <MeasurementGroup key={`${group.key}-${group.metric_name}-${group.definition_version}-${group.unit}`} group={group} population={result.cohort} definition={definitions.find((item) => item.name === group.metric_name)} onSelect={setSelected} filmHref={filmHref} />)}</div>}
        {result.exclusions.length ? <section aria-label="Excluded evidence"><h2 className="mb-2 text-sm font-semibold">Excluded from this analysis</h2><ul className="flex flex-col gap-1 text-sm text-muted-foreground">{result.exclusions.map((item) => <li key={item.reason}>{item.count} · {item.reason.replaceAll("_", " ")}</li>)}</ul></section> : null}
        {context.layout === "analyst" || tab === "analyst" ? <section className="border-t border-border-soft pt-4"><h2 className="text-base font-semibold">Dataset and definitions</h2><p className="mt-1 break-all text-xs text-muted-foreground">Dataset version: {result.dataset_version}</p><Button size="sm" variant="outline" className="mt-3" onClick={() => exportMeasurements(result)}>Export contributing measurements</Button><p className="mt-3 text-sm text-muted-foreground">Observed measurements, model classifications, predictions, and staff judgments retain separate definitions. Interaction and predictive results remain excluded until their evaluation gates pass.</p></section> : null}
      </> : null}
      {selected ? <section aria-label="Play evidence" className="border-t border-border-soft pt-5"><div className="flex items-center justify-between"><h2 className="text-lg font-semibold">Evidence for this measurement</h2><Button variant="ghost" size="sm" onClick={() => setSelected(null)}>Close evidence</Button></div>{evidenceError ? <p role="alert">{evidenceError}</p> : !evidence ? <p role="status">Loading play evidence…</p> : <><p className="mt-2 text-sm text-muted-foreground">Canonical play {evidence.play_id} · {evidence.views.length} camera view(s)</p><div className="mt-3 flex flex-wrap gap-2">{evidence.views.map((view) => <Button asChild key={view.clip_id} size="sm" variant="outline"><Link href={filmReviewHref(new URLSearchParams(params.toString()), view.clip_id, { from: `${pathname}?${params}` })}>{view.camera_label || "Watch evidence"} · {view.start_seconds.toFixed(1)}–{view.end_seconds.toFixed(1)}s</Link></Button>)}</div><details className="mt-4 text-xs"><summary className="cursor-pointer">Events, eligibility and provenance</summary><pre className="mt-2 max-h-80 overflow-auto whitespace-pre-wrap break-words rounded border border-border-soft p-3">{JSON.stringify(evidence, null, 2)}</pre></details></>}</section> : null}
      {tab === "analyst" && (currentRole === "admin" || currentRole === "analyst") ? <LearningPanel /> : null}
    </>}
  </div>;
}

function MeasurementGroup({ group, population, definition, onSelect, filmHref }: { group: AnalyticsGroup; population: AnalyticsResult["cohort"]; definition?: MetricDefinition; onSelect: (row: MetricEvidence) => void; filmHref: (row: MetricEvidence, rows: MetricEvidence[]) => string }) {
  const option = useMemo(() => ({
    textStyle: { color: "#b9c6d4", fontFamily: "system-ui" },
    grid: { left: 48, right: 20, top: 16, bottom: 45 },
    tooltip: { trigger: "axis" },
    xAxis: { type: "category", name: "Play sample", data: group.evidence.map((_, index) => String(index + 1)), axisLabel: { color: "#b9c6d4" } },
    yAxis: { type: "value", name: group.unit, scale: true, splitLine: { lineStyle: { color: "#2d3b4b" } } },
    series: [{ type: "bar", data: group.evidence.map((row) => row.value), itemStyle: { color: "#e4bb50" }, barMaxWidth: 28 }],
  }), [group]);
  return <section aria-label={definition?.label ?? group.metric_name} className="min-w-0 border-t border-border-soft pt-4">
    <div className="flex flex-wrap items-start justify-between gap-2"><div><h2 className="text-base font-semibold">{definition?.label ?? group.metric_name.replaceAll("_", " ")}</h2><p className="mt-1 text-xs text-muted-foreground">{group.key} · n={group.n} eligible · {group.unit} {group.low_sample ? "· Small sample" : ""}</p></div><span className="text-sm tabular-nums">Median {group.median.toFixed(2)} {group.unit}</span></div>
    <p className="mt-2 text-xs text-muted-foreground">Selected cohort: {population.plays} canonical plays · {population.eligible_measurements} eligible measurements · {population.unknown_count ?? 0} unknown. 10th–90th percentile: {group.p10.toFixed(2)}–{group.p90.toFixed(2)} {group.unit}. {definition?.definition} {definition ? `Definition ${group.definition_version ?? definition.version} · ${definition.value_kind}` : "Definition unavailable"}</p>
    <EChart option={option} label={`${definition?.label ?? group.metric_name}, ${group.n} eligible samples in ${group.unit}. Use the table to inspect evidence.`} onSelect={(index) => { const row = group.evidence[index]; if (row) onSelect(row); }} />
    <details><summary className="cursor-pointer text-sm font-medium">Contributing plays and accessible data table</summary><div className="mt-3 max-h-72 overflow-auto"><table className="w-full text-left text-xs tabular-nums"><caption className="sr-only">{definition?.label ?? group.metric_name} contributing measurements in {group.unit}</caption><thead><tr className="border-b border-border-soft"><th className="p-2">Sample</th><th className="p-2">Value ({group.unit})</th><th className="p-2">Evidence</th></tr></thead><tbody>{group.evidence.map((row, index) => <tr key={row.metric_id} className="border-b border-border-soft"><td className="p-2">{index + 1}</td><td className="p-2">{row.value.toFixed(2)}</td><td className="flex flex-wrap items-center gap-3 p-2"><Link className="text-primary underline underline-offset-2" href={filmHref(row, group.evidence)}>Watch play</Link><button type="button" className="text-primary underline underline-offset-2" onClick={() => onSelect(row)}>Inspect evidence</button></td></tr>)}</tbody></table></div></details>
  </section>;
}

function exportMeasurements(result: AnalyticsResult) {
  const quote = (value: unknown) => `"${String(value ?? "").replace(/^[=+@-]/, "'$&").replaceAll('"', '""')}"`;
  const seen = new Set<string>();
  const values = [...result.groups, ...suppliedCensoringGroups(result.censoring)].flatMap((group) => group.evidence.flatMap((row) => {
    const key = JSON.stringify([group.metric_name, group.key, group.definition_version, row.metric_id]);
    if (seen.has(key)) return [];
    seen.add(key);
    return [[result.dataset_version, group.metric_name, group.key, row.value, group.unit, row.right_censored ?? false, row.play_id, row.clip_id, row.start_seconds, row.end_seconds]];
  }));
  const rows = [["dataset_version", "metric", "group", "value", "unit", "right_censored", "canonical_play", "clip", "source_start_seconds", "source_end_seconds"], ...values];
  const url = URL.createObjectURL(new Blob([rows.map((row) => row.map(quote).join(",")).join("\r\n")], { type: "text/csv;charset=utf-8" }));
  const link = document.createElement("a"); link.href = url; link.download = "football-iq-measurements.csv"; link.click(); URL.revokeObjectURL(url);
}
