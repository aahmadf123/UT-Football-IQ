import { apiBase } from "./endpoints";
import { apiErrorFromResponse, offlineError } from "./api-error";
import type { AnalysisContext } from "./analysis-context";

export interface MetricDefinition {
  name: string; label: string; definition: string; unit: string; version: string;
  value_kind: string; availability: string; required_evidence?: string[]; applicability?: string | string[];
}
export interface MetricEvidence {
  play_id: string; clip_id: string; metric_id: string; value: number; video_id: string;
  start_seconds: number; end_seconds: number;
  player_id?: string | null; tracklet_id?: string | null; result_version?: string | null;
  right_censored?: boolean; subject?: string; metric_packet?: Record<string, unknown>;
  focus_seconds?: number;
}
export type FilmEvidence = Omit<MetricEvidence, "value">;
export interface PositionOpportunity {
  kind: string; definition_version: string; eligible_count: number; player_count: number;
  evidence: MetricEvidence[];
}
export interface SpatialProduct {
  metric_name: string; definition_version: string; unit: string; evidence: FilmEvidence[];
}
export interface AnalyticsGroup {
  key: string; metric_name: string; n: number; mean: number; median: number; p10: number; p90: number;
  min: number; max: number; unit: string; low_sample: boolean; evidence: MetricEvidence[];
  definition_version?: string; eligible_count?: number; unknown_count?: number;
}
export interface SurvivalStep { seconds: number; at_risk: number; events: number; censored: number; survival: number }
export interface CensoringGroup {
  key: string; metric_name: string; definition_version: string; n: number;
  events: number; censored: number; unit: string; evidence: MetricEvidence[];
  survival_curve: SurvivalStep[];
}
export interface AnalyticsResult {
  dataset_version: string;
  cohort: { plays: number; player_reps: number; eligible_measurements: number; excluded_measurements: number; unknown_count?: number };
  groups: AnalyticsGroup[]; censoring?: CensoringGroup[]; opportunities?: PositionOpportunity[];
  products?: SpatialProduct[]; exclusions: { reason: string; count: number }[];
}
export interface SavedAnalysisView {
  id: string; name: string; surface: "film" | "analytics"; filters: Partial<AnalysisContext>;
  visibility: "private" | "team"; user_id?: string; owner_id?: string; can_edit?: boolean;
}
export interface PlayEvidence {
  play_id: string;
  views: { clip_id: string; video_id: string; start_seconds: number; end_seconds: number; camera_label: string | null }[];
  metrics: { id: string; metric_name: string; metric_value: Record<string, unknown>; provenance?: Record<string, unknown> | null; eligibility?: unknown }[];
  events?: { id: string; event_type: string; timestamp_seconds: number | null; frame_number?: number | null }[];
  [key: string]: unknown;
}
async function request<T>(path: string, token?: string, body?: unknown, method = body == null ? "GET" : "POST"): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${apiBase()}/api/v1${path}`, {
      method, headers: { ...(token ? { Authorization: `Bearer ${token}` } : {}), ...(body != null ? { "Content-Type": "application/json" } : {}) },
      ...(body != null ? { body: JSON.stringify(body) } : {}),
    });
  } catch (cause) { throw offlineError("load analysis", cause); }
  if (!response.ok) throw await apiErrorFromResponse(response, "load analysis");
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}
export const fetchMetricDefinitions = (token?: string) => request<{ definitions: MetricDefinition[] }>("/metrics/definitions", token);
export const fetchPlayEvidence = (id: string, token?: string) => request<PlayEvidence>(`/plays/${encodeURIComponent(id)}/evidence`, token);
export const fetchSavedViews = (token?: string) => request<SavedAnalysisView[]>("/saved-views", token);
export const saveAnalysisView = (view: Omit<SavedAnalysisView, "id">, token?: string) => request<SavedAnalysisView>("/saved-views", token, view);
export const deleteAnalysisView = (id: string, token?: string) => request<void>(`/saved-views/${encodeURIComponent(id)}`, token, undefined, "DELETE");
export function queryAnalytics(context: AnalysisContext, token?: string): Promise<AnalyticsResult> {
  const filters: Record<string, unknown> = { group_by: context.group_by, minimum_sample: 5 };
  for (const key of ["session_kind", "player_id", "position", "library_role", "side_of_ball", "personnel", "formation", "situation", "drill_type", "contact_level", "opponent"] as const) if (context[key]) filters[key] = context[key];
  if (context.season) filters.season = Number(context.season);
  if (context.session_ids) filters.session_ids = context.session_ids.split(",").map((id) => id.trim()).filter(Boolean);
  if (context.since) filters.since = `${context.since.slice(0, 10)}T00:00:00Z`;
  if (context.until) filters.until = `${context.until.slice(0, 10)}T23:59:59.999999Z`;
  if (context.metric) filters.metric_names = [context.metric];
  return request<AnalyticsResult>("/analytics/query", token, filters);
}
