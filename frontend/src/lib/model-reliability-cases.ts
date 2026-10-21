import { fetchForRead } from "@/lib/api";
import { apiBase } from "@/lib/endpoints";
import { apiErrorFromResponse, offlineError } from "@/lib/api-error";
import { count, finite, record } from "@/lib/model-reliability";
import type { LearningEvaluation } from "@/lib/types";

export type HeldoutCase = Record<string, unknown> & { case_id: string; task: string; observed: number; predicted: number; baseline: number; error: number; baseline_error: number; lineage: Record<string, string>; observation_clock: { event: string; timestamp_seconds: number }; capture_regime: string; source_group: string; film_category: string; observed_outcome: string; error_bin: string; outcome_pair: string; feature_evidence_sha256: string[]; annotation_sha256: string; film: { clip_id: string; focus_seconds: number; source_export_job_id: string; source_export_sha256: string } | null; film_unavailable_reason: string | null };
export interface HeldoutCasePage { evaluation_id: string; model_id: string; artifact_sha256: string; case_sha256: string; case_count: number; selected_count: number; offset: number; groups: Record<string, { value: string; case_count: number }[]>; filter: { dimension: string | null; category: string | null }; known_source_group_count: number; canonical_play_count: number; minimum_sample_count: number; small_sample: boolean; descriptive_only: true; release_qualification_inherited: false; metrics: Record<string, unknown>; baseline_metrics: Record<string, unknown>; binary: boolean; cases: HeldoutCase[] }
export const CASE_DIMENSIONS: Record<string, string> = { capture_regime: "Capture regime", source_group: "Source game or session", film_category: "Confirmed film category", observed_outcome: "Observed outcome", error_bin: "Error interval", outcome_pair: "Observed / predicted class" };
const hash = (v: unknown) => typeof v === "string" && /^[0-9a-f]{64}$/.test(v);
export function validCasePage(value: unknown, report: LearningEvaluation, dimension: string, category: string, expectedOffset = 0, download = false): value is HeldoutCasePage {
  const page = record(value), descriptor = record(report.heldout_diagnostics);
  if (page.schema_version !== 1 || page.split !== "holdout" || page.evaluation_id !== report.id || page.model_id !== report.model_id || page.artifact_sha256 !== report.artifact_sha256 || page.case_sha256 !== descriptor.sha256 || !hash(page.case_sha256) || page.case_count !== report.sample_count || !count(page.case_count) || !count(page.selected_count) || page.selected_count > page.case_count || !count(page.offset) || page.offset !== expectedOffset || !count(page.known_source_group_count) || !count(page.canonical_play_count) || page.canonical_play_count !== page.selected_count || page.known_source_group_count > page.canonical_play_count || page.descriptive_only !== true || page.release_qualification_inherited !== false || typeof page.binary !== "boolean" || !Array.isArray(page.cases) || page.cases.length > page.selected_count || record(page.filter).dimension !== (dimension || null) || record(page.filter).category !== (category || null)) return false;
  if (page.binary !== ["completion","pressure"].includes(String(report.task)) || page.minimum_sample_count !== 20 || page.small_sample !== (page.selected_count < 20) || page.cases.length !== Math.min(download ? page.selected_count : 25, Math.max(0,page.selected_count-expectedOffset))) return false;
  const actualGroups = record(page.groups), expectedGroups = record(descriptor.groups);
  if (Object.keys(actualGroups).sort().join("|") !== Object.keys(CASE_DIMENSIONS).sort().join("|")) return false;
  for (const name of Object.keys(CASE_DIMENSIONS)) {
    const rows = actualGroups[name], expected = expectedGroups[name];
    if (!Array.isArray(rows) || !Array.isArray(expected) || rows.length !== expected.length || new Set(rows.map(row => record(row).value)).size !== rows.length || rows.some((row,i) => typeof record(row).value !== "string" || !count(record(row).case_count) || record(row).value !== record(expected[i]).value || record(row).case_count !== record(expected[i]).case_count) || rows.reduce((sum,row) => sum+Number(record(row).case_count),0) !== page.case_count) return false;
  }
  if (dimension && (!Array.isArray(actualGroups[dimension]) || (actualGroups[dimension] as unknown[]).map(record).find(row => row.value === category)?.case_count !== page.selected_count)) return false;
  const ids = new Set(), plays = new Set();
  for (const unknownCase of page.cases) {
    const c = record(unknownCase), clock = record(c.observation_clock), lineage = record(c.lineage), film = c.film === null ? null : record(c.film);
    if (!hash(c.case_id) || ids.has(c.case_id) || c.task !== report.task || c.split !== "holdout" || ![c.observed,c.predicted,c.baseline,c.error,c.baseline_error].every(finite) || Math.abs((c.predicted as number) - (c.observed as number) - (c.error as number)) > 1e-9 || Math.abs((c.baseline as number) - (c.observed as number) - (c.baseline_error as number)) > 1e-9 || !finite(clock.timestamp_seconds) || clock.timestamp_seconds < 0 || typeof clock.event !== "string" || !hash(lineage.media_sha256) || ![lineage.session_id,lineage.recording_id,lineage.canonical_play_id].every(v => typeof v === "string" && !!v) || !Array.isArray(c.feature_evidence_sha256) || !c.feature_evidence_sha256.length || !c.feature_evidence_sha256.every(hash) || !hash(c.annotation_sha256) || (dimension && c[dimension] !== category) || (film !== null && (typeof film.clip_id !== "string" || !finite(film.focus_seconds) || film.focus_seconds < 0 || !hash(film.source_export_sha256)))) return false;
    if (page.binary && (!([0,1].includes(c.observed as number)) || (c.predicted as number) < 0 || (c.predicted as number) > 1 || (c.baseline as number) < 0 || (c.baseline as number) > 1)) return false;
    if (plays.has(lineage.canonical_play_id)) return false;
    ids.add(c.case_id); plays.add(lineage.canonical_play_id);
  }
  return true;
}
export async function loadHeldoutCases(id: string, filter: { dimension: string; category: string; offset: number; download?: boolean }, token?: string): Promise<unknown> {
  const params = new URLSearchParams({ offset: String(filter.offset), limit: "25" });
  if (filter.dimension) { params.set("dimension", filter.dimension); params.set("category", filter.category); }
  if (filter.download) params.set("download", "true");
  let response: Response;
  try { response = await fetchForRead(`${apiBase()}/api/v1/learning/evaluations/${encodeURIComponent(id)}/cases?${params}`, { headers: token ? { Authorization: `Bearer ${token}` } : {} }); }
  catch (cause) { throw offlineError("load held-out cases", cause); }
  if (!response.ok) throw await apiErrorFromResponse(response, "load held-out cases");
  return response.json();
}
