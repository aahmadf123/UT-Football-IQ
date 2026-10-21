"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { CASE_DIMENSIONS, loadHeldoutCases, validCasePage, type HeldoutCasePage } from "@/lib/model-reliability-cases";
import { calibration, confusion, count, errors, finite, record } from "@/lib/model-reliability";
import { filmReviewHref } from "@/lib/analysis-context";
import { friendlyMessage } from "@/lib/api-error";
import type { LearningEvaluation } from "@/lib/types";
import { NativeSelect } from "@/components/ui/native-select";
import { Button } from "@/components/ui/button";

const number = (value: unknown) => finite(value) ? value.toFixed(3) : "Unavailable";
export function ModelReliabilityCases({ report, authToken }: { report: LearningEvaluation; authToken?: string }) {
  const descriptor = record(report.heldout_diagnostics), groups = record(descriptor.groups);
  const saved = () => {
    const params = new URLSearchParams(typeof window === "undefined" ? "" : window.location.search);
    const dimension = params.get("reliability_case_dimension") ?? "", category = params.get("reliability_case_category") ?? "", rawOffset = Number(params.get("reliability_case_offset") ?? "0");
    return params.get("reliability_evaluation") === report.id && ((!dimension && !category) || (dimension in CASE_DIMENSIONS && Array.isArray(groups[dimension]) && (groups[dimension] as unknown[]).some(value => record(value).value === category))) ? { dimension, category, offset: Number.isSafeInteger(rawOffset) && rawOffset >= 0 ? rawOffset : 0 } : { dimension:"", category:"", offset:0 };
  };
  const [dimension,setDimension] = useState(() => saved().dimension), [category,setCategory] = useState(() => saved().category), [offset,setOffset] = useState(() => saved().offset), [refresh,setRefresh] = useState(0), [returnTo,setReturnTo] = useState("");
  const persistFilter = useCallback((dimension: string, category: string, offset: number) => {
    const url = new URL(window.location.href);
    for (const [key,value] of Object.entries({ reliability_model:report.model_id, reliability_evaluation:report.id, reliability_case_dimension:dimension, reliability_case_category:category, reliability_case_offset:String(offset) })) url.searchParams.set(key,value);
    window.history.replaceState(window.history.state,"",`${url.pathname}${url.search}`);
  }, [report.model_id,report.id]);
  const [loaded,setLoaded] = useState<{ key: string; page: HeldoutCasePage } | null>(null), [error,setError] = useState<{ key: string; message: string } | null>(null), [busy,setBusy] = useState(false);
  const generation = useRef(0), key = JSON.stringify([report.id,descriptor.sha256,dimension,category,offset,refresh]);
  const storedEvaluation = report.source === "measured_human_holdout" && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(report.id);
  const available = storedEvaluation && descriptor.schema_version === 1 && count(descriptor.case_count) && descriptor.case_count === report.sample_count && typeof descriptor.sha256 === "string";
  useEffect(() => {
    if (!available) return;
    const current = ++generation.current;
    void loadHeldoutCases(report.id,{dimension,category,offset},authToken).then(value => {
      if (current !== generation.current) return;
      if (!validCasePage(value,report,dimension,category,offset)) throw new Error("Stored case diagnostics do not match this evaluation and filter.");
      persistFilter(dimension,category,offset);
      setReturnTo(window.location.pathname+window.location.search);
      setLoaded({key,page:value});
    }).catch(cause => { if (current === generation.current) setError({key,message:friendlyMessage(cause)}); });
    return () => { generation.current += 1; };
  }, [available,authToken,report,key,dimension,category,offset,persistFilter]);
  const page = loaded?.key === key ? loaded.page : null;
  const options = Array.isArray(groups[dimension]) ? (groups[dimension] as unknown[]).map(record).filter(row => typeof row.value === "string" && count(row.case_count)) : [];
  const failure = error?.key === key ? error.message : null;
  async function download() {
    setBusy(true);
    const current = generation.current;
    try {
      const value = await loadHeldoutCases(report.id,{dimension,category,offset:0,download:true},authToken);
      if (current !== generation.current) return;
      if (!validCasePage(value,report,dimension,category,0,true) || value.cases.length !== value.selected_count) throw new Error("The complete filtered case export is unavailable.");
      const url = URL.createObjectURL(new Blob([JSON.stringify(value,null,2)],{type:"application/json"})), link = document.createElement("a");
      link.href = url; link.download = `heldout-cases-${report.id.replace(/[^a-zA-Z0-9_-]/g,"_")}.json`; document.body.appendChild(link); link.click(); link.remove(); window.setTimeout(() => URL.revokeObjectURL(url),1000);
    } catch (cause) { setError({key,message:friendlyMessage(cause)}); } finally { setBusy(false); }
  }
  if (!available && descriptor.schema_version === 1 && !storedEvaluation) return <section aria-label="Held-out case diagnostics"><h4 className="font-medium">Held-out cases and category comparisons</h4><p>Case drilldown requires a stored, independently verified evaluation. Raw candidate case data cannot establish release evidence.</p></section>;
  if (!available) return <section aria-label="Held-out case diagnostics"><h4 className="font-medium">Held-out cases and category comparisons</h4><p>No frozen case evidence was stored in this report. Aggregate scores cannot reconstruct source cases.</p></section>;
  const metrics = record(page?.metrics), baseline = record(page?.baseline_metrics);
  const matrix = page ? confusion(metrics.confusion_matrix,page.selected_count) : null;
  const distribution = page ? errors(metrics.error_distribution,page.selected_count,page.binary) : null;
  const bins = page ? calibration(metrics.calibration_bins,page.selected_count,page.binary) : null;
  const baselineBins = page ? calibration(baseline.calibration_bins,page.selected_count,page.binary) : null;
  return <section aria-label="Held-out case diagnostics" className="space-y-3 border-t border-border-soft pt-4"><h4 className="font-medium">Held-out cases and category comparisons</h4><p>Descriptive comparisons only. Filters do not inherit release approval. The reference stays the frozen training baseline; it is never refitted on selected outcomes.</p><p>Task: {String(report.task)}. {report.task === "completion" || report.task === "pressure" ? "Predictions and baseline use probability (0–1); observations are binary 0/1; signed errors use probability units." : "Observed outcomes, predictions, baseline and signed errors use yards."} Signed error means prediction minus observation.</p><div className="flex flex-wrap gap-3"><label>Case dimension<NativeSelect aria-label="Held-out case dimension" value={dimension} onChange={event => { const next = event.target.value; setDimension(next); const rows = groups[next]; const category = Array.isArray(rows) && typeof record(rows[0]).value === "string" ? String(record(rows[0]).value) : ""; setCategory(category); setOffset(0); persistFilter(next,category,0); }}><option value="">All held-out cases</option>{Object.entries(CASE_DIMENSIONS).filter(([value]) => Array.isArray(groups[value])).map(([value,label]) => <option key={value} value={value}>{label}</option>)}</NativeSelect></label>{dimension ? <label>Case category<NativeSelect aria-label="Held-out case category" value={category} onChange={event => {setCategory(event.target.value);setOffset(0);persistFilter(dimension,event.target.value,0);}}>{options.map(row => <option key={String(row.value)} value={String(row.value)}>{String(row.value)} ({String(row.case_count)})</option>)}</NativeSelect></label> : null}<Button size="sm" variant="outline" onClick={() => setRefresh(value => value+1)}>Reload held-out cases</Button><Button size="sm" variant="outline" disabled={!page || busy} onClick={() => void download()}>{busy ? "Downloading cases..." : "Download selected cases"}</Button></div>{failure ? <p role="alert">{failure}</p> : !page ? <p role="status">Loading frozen held-out cases...</p> : null}{page ? <><p>{page.selected_count} of {page.case_count} held-out plays; {page.canonical_play_count} distinct canonical plays; {page.known_source_group_count} known source game/session groups. These group IDs alone do not attest independence.</p>{page.small_sample ? <p>Small descriptive slice: fewer than {page.minimum_sample_count} plays. Qualification and confidence intervals are unavailable.</p> : <p>No confidence interval was estimated for this slice; sample count alone does not qualify release.</p>}<p className="break-all text-xs">Case SHA-256 {page.case_sha256}</p><table className="w-full text-left"><caption className="text-left">Selected category against frozen baseline</caption><thead><tr><th>Measure</th><th>Model</th><th>Baseline</th></tr></thead><tbody>{["brier","log_loss","ece","mae","rmse","mean_error"].filter(name => metrics[name] !== undefined).map(name => <tr key={name}><th scope="row">{name.replaceAll("_"," ")}</th><td>{number(metrics[name])}</td><td>{number(baseline[name])}</td></tr>)}</tbody></table>{bins ? <table className="w-full text-left"><caption className="text-left">Selected category calibration</caption><thead><tr><th>Source</th><th>Plays</th><th>Predicted mean</th><th>Observed mean</th></tr></thead><tbody>{[["Model",bins],["Baseline",baselineBins]].flatMap(([label,rows]) => Array.isArray(rows) ? rows.map((row,i) => <tr key={`${label}-${i}`}><th scope="row">{String(label)} bin {i+1}</th><td>{row.count}</td><td>{number(row.predicted_mean)}</td><td>{number(row.observed_mean)}</td></tr>) : [])}</tbody></table> : null}{distribution ? <table className="w-full text-left"><caption className="text-left">Selected category signed-error distribution</caption><thead><tr><th>Error interval</th><th>Plays</th></tr></thead><tbody>{distribution.map((row,i) => <tr key={i}><th scope="row">{row.lower ?? "below"} to {row.upper ?? "above"}</th><td>{row.count}</td></tr>)}</tbody></table> : null}{matrix ? <table className="w-full text-left"><caption className="text-left">Selected category confusion: observed rows, predicted columns</caption><thead><tr><th>Observed / predicted</th>{matrix.labels.map(label => <th key={label}>{label}</th>)}</tr></thead><tbody>{matrix.labels.map((label,i) => <tr key={label}><th scope="row">{label}</th>{matrix.counts[i].map((value,j) => <td key={j}>{value}</td>)}</tr>)}</tbody></table> : null}<div className="overflow-x-auto"><table className="w-full text-left"><caption className="text-left">Frozen held-out source cases</caption><thead><tr><th>Source and clock</th><th>Observed</th><th>Predicted</th><th>Baseline</th><th>Signed error</th><th>Film and evidence</th></tr></thead><tbody>{page.cases.map(c => <tr key={c.case_id}><th scope="row" className="max-w-48 break-all">{c.source_group}<br />{c.observation_clock.event} at {number(c.observation_clock.timestamp_seconds)} source seconds<br />{c.capture_regime} / {c.film_category}</th><td>{number(c.observed)}</td><td>{number(c.predicted)}</td><td>{number(c.baseline)}</td><td>{number(c.error)}</td><td>{c.film ? <Link className="text-primary underline" href={filmReviewHref(new URLSearchParams(returnTo.split("?")[1] ?? ""),c.film.clip_id,{t:String(c.film.focus_seconds),from:returnTo || "/team-stats/"})}>Watch exact source evidence</Link> : <p>{c.film_unavailable_reason}</p>}<details><summary>Source case and evidence hashes</summary><pre className="max-h-64 overflow-auto whitespace-pre-wrap break-all text-xs">{JSON.stringify(c,null,2)}</pre></details></td></tr>)}</tbody></table></div><div className="flex gap-2"><Button size="sm" variant="outline" disabled={offset===0} onClick={() => { const next = Math.max(0,offset-25); setOffset(next); persistFilter(dimension,category,next); }}>Previous cases</Button><Button size="sm" variant="outline" disabled={offset+page.cases.length>=page.selected_count} onClick={() => { const next = offset+25; setOffset(next); persistFilter(dimension,category,next); }}>Next cases</Button></div></> : null}</section>;
}
