/**
 * Football-IQ API client — upload pipeline, library, inbox, alerts, clips.
 *
 * Environment variables consumed (public, browser-safe):
 *   NEXT_PUBLIC_API_URL    — backend base URL (FastAPI)
 *   NEXT_PUBLIC_WORKER_URL — Cloudflare Worker base URL
 */

import type {
  ApiClip,
  ApiJob,
  ApiPlayer,
  ApiPracticeSessionGroup,
  ApiVideo,
  CfbdMacBenchmarkResponse,
  CfbdScheduleResponse,
  CfbdTeamResponse,
  ConceptSearchResponse,
  ClipOverlayPayload,
  OpponentSummary,
  ReportCreateRequest,
  ReportDownloadResponse,
  ReportJob,
  SelfScoutResponse,
  SessionKind,
  SourceType,
  SystemSettingsResponse,
  SystemSettingsUpdate,
  UserPreferences,
  OurPossession,
  WorkloadGatedDetail,
} from "./types";
import { apiBase } from "./endpoints";

// ── Helpers ──────────────────────────────────────────────────────────────────

function authHeaders(token?: string): Record<string, string> {
  const h: Record<string, string> = { "Content-Type": "application/json" };
  if (token) h["Authorization"] = `Bearer ${token}`;
  return h;
}

function getHeaders(token?: string): Record<string, string> {
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function getJSON<T>(url: string, token?: string): Promise<T> {
  const res = await fetch(url, { headers: getHeaders(token) });
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`GET ${url} failed (${res.status}): ${body}`);
  }
  return res.json() as Promise<T>;
}

// ── Upload URL ───────────────────────────────────────────────────────────────

export interface UploadUrlResponse {
  uploadUrl: string;
  key: string;
}

async function _requestUploadUrlFrom(
  base: string,
  filename: string,
  token?: string,
): Promise<UploadUrlResponse> {
  const res = await fetch(`${base}/api/v1/videos/upload-url`, {
    method: "POST",
    headers: authHeaders(token),
    body: JSON.stringify({ filename }),
  });
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`Failed to get upload URL (${res.status}): ${body}`);
  }
  return res.json() as Promise<UploadUrlResponse>;
}

export async function requestUploadUrl(
  filename: string,
  token?: string,
): Promise<UploadUrlResponse> {
  const workerUrl = process.env.NEXT_PUBLIC_WORKER_URL;
  const apiUrl = apiBase();

  // Try the Worker first when one is explicitly configured (this is the
  // E2E/prod path). If the Worker rejects the token (JWT secret mismatch) or
  // is unreachable, fall back to the backend, which exposes the same contract.
  if (workerUrl) {
    try {
      return await _requestUploadUrlFrom(workerUrl, filename, token);
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      if (!apiUrl || (!message.includes("401") && !message.includes("403"))) {
        throw err;
      }
      // Fall through to backend on auth failure.
    }
  }

  if (!apiUrl) {
    throw new Error(
      "No upload endpoint configured: set NEXT_PUBLIC_WORKER_URL (edge Worker) " +
        "or NEXT_PUBLIC_API_URL (backend fallback).",
    );
  }
  return _requestUploadUrlFrom(apiUrl, filename, token);
}

// ── R2 Upload ────────────────────────────────────────────────────────────────

export interface R2UploadResult {
  key: string;
  size: number;
  etag: string;
  storageUri: string;
}

export async function uploadToR2(
  uploadUrl: string,
  file: File,
  token?: string,
  onProgress?: (loaded: number, total: number) => void,
): Promise<R2UploadResult> {
  if (onProgress && typeof XMLHttpRequest !== "undefined") {
    return uploadWithXhr(uploadUrl, file, token, onProgress);
  }
  const headers: Record<string, string> = {
    "Content-Type": file.type || "video/mp4",
  };
  if (token) headers["Authorization"] = `Bearer ${token}`;

  const res = await fetch(uploadUrl, {
    method: "PUT",
    headers,
    body: file,
  });
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`R2 upload failed (${res.status}): ${body}`);
  }
  return res.json() as Promise<R2UploadResult>;
}

function uploadWithXhr(
  url: string,
  file: File,
  token: string | undefined,
  onProgress: (loaded: number, total: number) => void,
): Promise<R2UploadResult> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", url);
    xhr.setRequestHeader("Content-Type", file.type || "video/mp4");
    if (token) xhr.setRequestHeader("Authorization", `Bearer ${token}`);

    xhr.upload.addEventListener("progress", (e) => {
      if (e.lengthComputable) onProgress(e.loaded, e.total);
    });

    xhr.addEventListener("load", () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          resolve(JSON.parse(xhr.responseText) as R2UploadResult);
        } catch {
          reject(new Error("Invalid JSON response from R2 upload"));
        }
      } else {
        reject(new Error(`R2 upload failed (${xhr.status}): ${xhr.responseText}`));
      }
    });

    xhr.addEventListener("error", () => reject(new Error("R2 upload network error")));
    xhr.addEventListener("abort", () => reject(new Error("R2 upload aborted")));
    xhr.send(file);
  });
}

// ── Video Registration ───────────────────────────────────────────────────────

export interface RegisterVideoRequest {
  filename: string;
  storage_uri: string;
  recorded_at?: string | null;
  session_kind?: SessionKind | null;
  source_type?: SourceType | null;
  opponent_team?: string | null;
  practice_session_id?: string | null;
  our_possession?: OurPossession | null;
}

export async function registerVideo(
  data: RegisterVideoRequest,
  token?: string,
): Promise<ApiVideo> {
  const base = apiBase();
  if (!base) throw new Error("NEXT_PUBLIC_API_URL is not configured");
  const res = await fetch(`${base}/api/v1/videos`, {
    method: "POST",
    headers: authHeaders(token),
    body: JSON.stringify(data),
  });
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`Video registration failed (${res.status}): ${body}`);
  }
  return res.json() as Promise<ApiVideo>;
}

// ── Inbox Status ─────────────────────────────────────────────────────────────

export interface VideoInboxItem {
  video_id: string;
  filename: string;
  video_status: string;
  total_jobs: number;
  running_jobs: number;
  succeeded_jobs: number;
  failed_jobs: number;
  clip_count: number;
  // Clips still on the same-session first pass, awaiting nightly upgrade
  // (Issue #147). Optional so older backends/mocks stay valid.
  preliminary_clip_count?: number;
  calibration_safe_pct: number | null;
  latest_error_stage: string | null;
  latest_error_message: string | null;
  same_session_job_count: number;
  pose_pipeline_active: boolean;
  created_at: string;
}

export async function fetchInboxStatus(
  token?: string,
  includeSucceeded = false,
): Promise<VideoInboxItem[]> {
  const base = apiBase();
  if (!base) throw new Error("NEXT_PUBLIC_API_URL is not configured");
  const params = new URLSearchParams();
  if (includeSucceeded) params.set("include_succeeded", "true");
  const qs = params.toString();
  const url = `${base}/api/v1/inbox/status${qs ? `?${qs}` : ""}`;
  const res = await fetch(url, { headers: authHeaders(token) });
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`Inbox status failed (${res.status}): ${body}`);
  }
  return res.json() as Promise<VideoInboxItem[]>;
}

// ── Library: practice sessions and videos ─────────────────────────────────────

export interface PracticeSessionFilters {
  recorded_after?: string;
  recorded_before?: string;
  session_kind?: SessionKind;
  opponent_team?: string;
  limit?: number;
}

export async function fetchPracticeSessions(
  filters: PracticeSessionFilters = {},
  token?: string,
): Promise<ApiPracticeSessionGroup[]> {
  const base = apiBase();
  if (!base) throw new Error("NEXT_PUBLIC_API_URL is not configured");
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(filters)) {
    if (v !== undefined && v !== null && v !== "") params.set(k, String(v));
  }
  const qs = params.toString();
  return getJSON<ApiPracticeSessionGroup[]>(
    `${base}/api/v1/practice-sessions${qs ? `?${qs}` : ""}`,
    token,
  );
}

export interface VideoFilters {
  recorded_after?: string;
  recorded_before?: string;
  session_kind?: SessionKind;
  source_type?: SourceType;
  opponent_team?: string;
  practice_session_id?: string;
  status?: string;
  limit?: number;
  offset?: number;
}

export async function fetchVideos(
  filters: VideoFilters = {},
  token?: string,
): Promise<ApiVideo[]> {
  const base = apiBase();
  if (!base) throw new Error("NEXT_PUBLIC_API_URL is not configured");
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(filters)) {
    if (v !== undefined && v !== null && v !== "") params.set(k, String(v));
  }
  const qs = params.toString();
  return getJSON<ApiVideo[]>(
    `${base}/api/v1/videos${qs ? `?${qs}` : ""}`,
    token,
  );
}

export async function fetchVideo(videoId: string, token?: string): Promise<ApiVideo> {
  const base = apiBase();
  if (!base) throw new Error("NEXT_PUBLIC_API_URL is not configured");
  return getJSON<ApiVideo>(`${base}/api/v1/videos/${videoId}`, token);
}

export interface JobFilters {
  status?: string;
  job_type?: string;
  video_id?: string;
  limit?: number;
}

export async function fetchJobs(filters: JobFilters = {}, token?: string): Promise<ApiJob[]> {
  const base = apiBase();
  if (!base) throw new Error("NEXT_PUBLIC_API_URL is not configured");
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(filters)) {
    if (v !== undefined && v !== null && v !== "") params.set(k, String(v));
  }
  const qs = params.toString();
  return getJSON<ApiJob[]>(`${base}/api/v1/jobs${qs ? `?${qs}` : ""}`, token);
}

export async function fetchClipsForVideo(
  videoId: string,
  token?: string,
): Promise<ApiClip[]> {
  const base = apiBase();
  if (!base) throw new Error("NEXT_PUBLIC_API_URL is not configured");
  return getJSON<ApiClip[]>(`${base}/api/v1/videos/${videoId}/clips`, token);
}

export async function fetchClip(clipId: string, token?: string): Promise<ApiClip> {
  const base = apiBase();
  if (!base) throw new Error("NEXT_PUBLIC_API_URL is not configured");
  return getJSON<ApiClip>(`${base}/api/v1/clips/${clipId}`, token);
}

// ── Clip overlays (Issue #104) ───────────────────────────────────────────────

export async function fetchClipOverlays(
  clipId: string,
  token?: string,
): Promise<ClipOverlayPayload> {
  const base = apiBase();
  if (!base) throw new Error("NEXT_PUBLIC_API_URL is not configured");
  return getJSON<ClipOverlayPayload>(
    `${base}/api/v1/clips/${clipId}/overlays`,
    token,
  );
}

// ── Self-Scout + Opponent Scout (Issue #105) ─────────────────────────────────

export async function fetchSelfScoutTendencies(
  videoId?: string | null,
  token?: string,
  limit?: number,
): Promise<SelfScoutResponse> {
  const base = apiBase();
  if (!base) throw new Error("NEXT_PUBLIC_API_URL is not configured");
  const params = new URLSearchParams();
  if (videoId) params.set("video_id", videoId);
  if (limit != null) params.set("limit", String(limit));
  const qs = params.toString();
  return getJSON<SelfScoutResponse>(
    `${base}/api/v1/self-scout/tendencies${qs ? `?${qs}` : ""}`,
    token,
  );
}

export async function fetchOpponentTendencies(
  opponentVideoId: string,
  token?: string,
  limit?: number,
): Promise<SelfScoutResponse> {
  const base = apiBase();
  if (!base) throw new Error("NEXT_PUBLIC_API_URL is not configured");
  const params = new URLSearchParams({ opponent_video_id: opponentVideoId });
  if (limit != null) params.set("limit", String(limit));
  return getJSON<SelfScoutResponse>(
    `${base}/api/v1/self-scout/opponent-tendencies?${params.toString()}`,
    token,
  );
}

export async function fetchOpponents(token?: string): Promise<OpponentSummary[]> {
  const base = apiBase();
  if (!base) throw new Error("NEXT_PUBLIC_API_URL is not configured");
  return getJSON<OpponentSummary[]>(`${base}/api/v1/opponents`, token);
}

// ── Players ──────────────────────────────────────────────────────────────────

export interface PlayerFilters {
  position?: string;
  position_group?: string;
  is_active?: boolean;
  jersey_number?: number;
  search?: string;
  limit?: number;
  offset?: number;
}

export async function fetchPlayers(
  filters: PlayerFilters = {},
  token?: string,
): Promise<ApiPlayer[]> {
  const base = apiBase();
  if (!base) throw new Error("NEXT_PUBLIC_API_URL is not configured");
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(filters)) {
    if (v !== undefined && v !== null && v !== "") params.set(k, String(v));
  }
  const qs = params.toString();
  return getJSON<ApiPlayer[]>(
    `${base}/api/v1/players${qs ? `?${qs}` : ""}`,
    token,
  );
}

export async function fetchPlayer(playerId: string, token?: string): Promise<ApiPlayer> {
  const base = apiBase();
  if (!base) throw new Error("NEXT_PUBLIC_API_URL is not configured");
  return getJSON<ApiPlayer>(`${base}/api/v1/players/${playerId}`, token);
}

// ── Metrics + coach corrections ──────────────────────────────────────────────

export interface ApiMetric {
  id: string;
  clip_id: string;
  tracklet_id: string | null;
  metric_name: string;
  metric_value: Record<string, unknown>;
  unit: string | null;
  is_suppressed: boolean;
  suppression_reason: string | null;
  experimental_flag: boolean;
  analytics_safe: boolean;
  confidence: number | null;
  effort_zscore: number | null;
  loaf_flag: boolean | null;
  evidence_uri: string | null;
  model_version_id: string | null;
  calibration_version_id: string | null;
  job_id: string | null;
  created_at: string;
}

export interface MetricFilters {
  clip_id?: string;
  tracklet_id?: string;
  metric_name?: string;
  experimental?: boolean;
  analytics_safe?: boolean;
  player_id?: string;
  limit?: number;
  offset?: number;
}

export async function fetchMetrics(
  filters: MetricFilters = {},
  token?: string,
): Promise<ApiMetric[]> {
  const base = apiBase();
  if (!base) throw new Error("NEXT_PUBLIC_API_URL is not configured");
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(filters)) {
    if (v !== undefined && v !== null && v !== "") params.set(k, String(v));
  }
  const qs = params.toString();
  return getJSON<ApiMetric[]>(
    `${base}/api/v1/metrics${qs ? `?${qs}` : ""}`,
    token,
  );
}

// ── Workload risk (Issue #149) ───────────────────────────────────────────────

export interface InjuryRiskFilters {
  date?: string;
  days?: number;
  position_group?: string;
}

export async function fetchInjuryRisk(
  filters: InjuryRiskFilters = {},
  token?: string,
): Promise<import("./health-workload").InjuryRiskResponse> {
  const base = apiBase();
  if (!base) throw new Error("NEXT_PUBLIC_API_URL is not configured");
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(filters)) {
    if (v !== undefined && v !== null && v !== "") params.set(k, String(v));
  }
  const qs = params.toString();
  return getJSON(`${base}/api/v1/health-workload/injury-risk${qs ? `?${qs}` : ""}`, token);
}

export async function fetchHealthDashboard(
  filters: InjuryRiskFilters = {},
  token?: string,
): Promise<import("./health-workload").HealthDashboardResponse> {
  const base = apiBase();
  if (!base) throw new Error("NEXT_PUBLIC_API_URL is not configured");
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(filters)) {
    if (v !== undefined && v !== null && v !== "") params.set(k, String(v));
  }
  const qs = params.toString();
  return getJSON(`${base}/api/v1/health-workload/dashboard${qs ? `?${qs}` : ""}`, token);
}

export type CorrectionType =
  | "clip_boundary"
  | "player_identity"
  | "event_tag"
  | "formation_tag"
  | "route_tag"
  | "coverage_tag"
  | "personnel_tag"
  | "leverage_tag"
  | "effort_tag"
  | "pose_biomechanics_tag";

export interface CorrectionCreate {
  clip_id: string;
  correction_type: CorrectionType;
  original_value?: Record<string, unknown> | null;
  corrected_value: Record<string, unknown>;
  notes?: string | null;
  training_eligible?: boolean;
}

export interface CorrectionResponse extends CorrectionCreate {
  id: string;
  corrected_by: string;
  exported_as_label: boolean;
  created_at: string;
  original_value: Record<string, unknown> | null;
  notes: string | null;
  training_eligible: boolean;
}

export async function createCorrection(
  body: CorrectionCreate,
  token?: string,
): Promise<CorrectionResponse> {
  const base = apiBase();
  if (!base) throw new Error("NEXT_PUBLIC_API_URL is not configured");
  const res = await fetch(`${base}/api/v1/corrections`, {
    method: "POST",
    headers: authHeaders(token),
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const errorBody = await res.text();
    throw new Error(`Correction request failed (${res.status}): ${errorBody}`);
  }
  return res.json() as Promise<CorrectionResponse>;
}

// ── Jobs ─────────────────────────────────────────────────────────────────────

export async function retryJob(jobId: string, token?: string): Promise<unknown> {
  const base = apiBase();
  if (!base) throw new Error("NEXT_PUBLIC_API_URL is not configured");
  const res = await fetch(`${base}/api/v1/jobs/${jobId}/retry`, {
    method: "POST",
    headers: authHeaders(token),
  });
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`Job retry failed (${res.status}): ${body}`);
  }
  return res.json();
}

// ── Process Film (ADR 0003 / Issue #187) ──────────────────────────────────────

export type ProcessingMode = "same_session" | "nightly";

export interface ProcessVideoResult {
  jobId: string;
  status: string;
}

/**
 * Raised when the backend returns ``503 workload_gated`` for a processing
 * request. Carries the structured workload detail so the UI can show a
 * coach-readable "system is busy" message instead of a raw error.
 */
export class WorkloadGatedError extends Error {
  readonly detail: WorkloadGatedDetail | null;
  constructor(message: string, detail: WorkloadGatedDetail | null) {
    super(message);
    this.name = "WorkloadGatedError";
    this.detail = detail;
  }
}

/**
 * Request processing for an uploaded video by creating a ``pipeline`` job
 * (the full orchestrated chain: ingest → detect → track → … → render)
 * through the **backend** job API (``POST /api/v1/jobs``). This is the
 * sanctioned, workload-gated entry point — it does not bypass the backend job
 * API or the Worker/R2 flow, and it never calls an external API. Defaults to
 * nightly (full-quality) priority; ``same_session`` (priority 10) is the
 * period-break fast path.
 */
export async function requestVideoProcessing(
  videoId: string,
  options?: { mode?: ProcessingMode },
  token?: string,
): Promise<ProcessVideoResult> {
  const base = apiBase();
  if (!base) throw new Error("NEXT_PUBLIC_API_URL is not configured");
  const mode: ProcessingMode = options?.mode ?? "nightly";
  const priority = mode === "same_session" ? 10 : 0;
  const res = await fetch(`${base}/api/v1/jobs`, {
    method: "POST",
    headers: authHeaders(token),
    body: JSON.stringify({
      video_id: videoId,
      job_type: "pipeline",
      priority,
      pipeline_mode: mode,
    }),
  });
  if (res.status === 503) {
    let detail: WorkloadGatedDetail | null = null;
    try {
      const body = (await res.json()) as { detail?: WorkloadGatedDetail };
      detail = body?.detail ?? null;
    } catch {
      detail = null;
    }
    throw new WorkloadGatedError(
      detail?.message ?? "The processing queue is busy right now. Try again shortly.",
      detail,
    );
  }
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`Process film request failed (${res.status}): ${body}`);
  }
  const job = (await res.json()) as { id: string; status: string };
  return { jobId: job.id, status: job.status };
}

// ── Alerts ───────────────────────────────────────────────────────────────────

export interface ApiAlert {
  id: string;
  player_id: string | null;
  clip_id: string | null;
  position_group: string;
  alert_type: string;
  severity: string;
  confidence: number;
  metric_name: string;
  metric_value: Record<string, unknown>;
  deviation_sd: number | null;
  clip_uri: string | null;
  period_name: string | null;
  session_id: string | null;
  job_id: string | null;
  is_acknowledged: boolean;
  acknowledged_by: string | null;
  acknowledged_at: string | null;
  is_actioned: boolean;
  actioned_by: string | null;
  actioned_at: string | null;
  created_at: string;
}

export interface AlertFilters {
  alert_type?: string;
  severity?: string;
  session_id?: string;
  acknowledged?: boolean;
  actioned?: boolean;
  limit?: number;
  offset?: number;
}

export async function fetchAlerts(
  filters: AlertFilters = {},
  token?: string,
): Promise<ApiAlert[]> {
  const base = apiBase();
  if (!base) throw new Error("NEXT_PUBLIC_API_URL is not configured");
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(filters)) {
    if (v !== undefined && v !== null && v !== "") params.set(k, String(v));
  }
  const qs = params.toString();
  return getJSON<ApiAlert[]>(
    `${base}/api/v1/alerts${qs ? `?${qs}` : ""}`,
    token,
  );
}

/** Mark an alert as actioned (turned into a practice/scheme follow-up, #137). */
export async function actionAlert(alertId: string, token?: string): Promise<ApiAlert> {
  const base = apiBase();
  if (!base) throw new Error("NEXT_PUBLIC_API_URL is not configured");
  const res = await fetch(`${base}/api/v1/alerts/${alertId}/action`, {
    method: "PATCH",
    headers: authHeaders(token),
  });
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`Action alert failed (${res.status}): ${body}`);
  }
  return res.json() as Promise<ApiAlert>;
}

// ── Tendency-break alerts (Issue #137) ───────────────────────────────────────

export interface TendencyBreakAlert {
  id: string | null;
  grouping_key: string;
  tendency_kind: string; // "season_tendency" | "pattern_break"
  lean: string; // "run" | "pass"
  severity: string;
  confidence: number;
  message: string;
  run_rate: number;
  pass_rate: number;
  total_plays: number;
  evidence_clip_ids: string[];
  source: string;
  experimental: boolean;
  baseline_pass_rate: number | null;
  divergence_pp: number | null;
  baseline_source: string | null;
  session_id: string | null;
  is_actioned: boolean;
  actioned_at: string | null;
}

export interface TendencyBreakListResponse {
  alerts: TendencyBreakAlert[];
  total: number;
}

export interface TendencyBreakRunResponse {
  generated: number;
  season_tendency_count: number;
  pattern_break_count: number;
  season_clip_count: number;
  game_clip_count: number;
  cfbd_baseline_pass_rate: number | null;
  cfbd_baseline_source: string | null;
  alerts: TendencyBreakAlert[];
}

export async function fetchTendencyBreakAlerts(
  filters: { actioned?: boolean; session_id?: string; limit?: number } = {},
  token?: string,
): Promise<TendencyBreakListResponse> {
  const base = apiBase();
  if (!base) throw new Error("NEXT_PUBLIC_API_URL is not configured");
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(filters)) {
    if (v !== undefined && v !== null && v !== "") params.set(k, String(v));
  }
  const qs = params.toString();
  return getJSON<TendencyBreakListResponse>(
    `${base}/api/v1/self-scout/tendency-break${qs ? `?${qs}` : ""}`,
    token,
  );
}

export async function generateTendencyBreak(
  opts: { game_video_id?: string; cfbd_team?: string } = {},
  token?: string,
): Promise<TendencyBreakRunResponse> {
  const base = apiBase();
  if (!base) throw new Error("NEXT_PUBLIC_API_URL is not configured");
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(opts)) {
    if (v !== undefined && v !== null && v !== "") params.set(k, String(v));
  }
  const qs = params.toString();
  const res = await fetch(
    `${base}/api/v1/self-scout/tendency-break${qs ? `?${qs}` : ""}`,
    { method: "POST", headers: authHeaders(token) },
  );
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`Generate tendency-break failed (${res.status}): ${body}`);
  }
  return res.json() as Promise<TendencyBreakRunResponse>;
}

// ── Frontier analytics (Issue #10) — experimental xSep/xYards/xPressure ───────

export interface FrontierMetric {
  id: string;
  clip_id: string;
  tracklet_id: string | null;
  metric_name: string;
  metric_value: Record<string, unknown>;
  unit: string | null;
  experimental: boolean;
  analytics_safe: boolean;
  confidence: number | null;
  source: string;
  sample_size: number | null;
  stability_note: string | null;
  position_group: string | null;
  created_at: string;
}

export interface FrontierMetricsResponse {
  metrics: FrontierMetric[];
  total: number;
  experimental: boolean;
  definitions: Record<
    string,
    { label: string; definition: string; interpretation: string; depends_on: string }
  >;
  note: string;
}

export interface FrontierFilters {
  metric_name?: string;
  clip_id?: string;
  position_group?: string;
  formation?: string;
  concept?: string;
  opponent?: string;
  limit?: number;
}

export async function fetchFrontierMetrics(
  filters: FrontierFilters = {},
  token?: string,
): Promise<FrontierMetricsResponse> {
  const base = apiBase();
  if (!base) throw new Error("NEXT_PUBLIC_API_URL is not configured");
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(filters)) {
    if (v !== undefined && v !== null && v !== "") params.set(k, String(v));
  }
  const qs = params.toString();
  return getJSON<FrontierMetricsResponse>(
    `${base}/api/v1/analytics/frontier${qs ? `?${qs}` : ""}`,
    token,
  );
}

// ── Zero-shot concept search (Issue #144) ────────────────────────────────────

export interface ConceptSearchFilters {
  k?: number;
  include_experimental?: boolean;
  opponent?: string;
  side_of_ball?: string;
}

/**
 * Zero-shot concept search: resolve a free-text football concept query
 * ("mesh", "cover 3 trips") against the structured labels the backend already
 * trusts, optionally expanded with EXPERIMENTAL play-embedding similarity.
 * Results are always approximate — the caller must label them as such.
 */
export async function searchConcepts(
  query: string,
  filters: ConceptSearchFilters = {},
  token?: string,
): Promise<ConceptSearchResponse> {
  const base = apiBase();
  if (!base) throw new Error("NEXT_PUBLIC_API_URL is not configured");
  const params = new URLSearchParams({ q: query });
  for (const [k, v] of Object.entries(filters)) {
    if (v !== undefined && v !== null && v !== "") params.set(k, String(v));
  }
  return getJSON<ConceptSearchResponse>(
    `${base}/api/v1/concept-search?${params.toString()}`,
    token,
  );
}

export type AlertStreamEvent =
  | { type: "alert"; alert: ApiAlert }
  | { type: "connected"; connection_id?: string }
  | { type: "keepalive" };

export interface AlertStreamHandle {
  close: () => void;
}

/**
 * Subscribe to the alert stream via SSE.
 *
 * Uses fetch + ReadableStream (not native EventSource) so we can set the
 * `Authorization: Bearer <token>` header that the backend `HTTPBearer`
 * dependency requires. Access tokens are deliberately NOT placed in the URL
 * (avoids leakage via logs, browser history, and referrers).
 */
export function subscribeAlerts(
  onEvent: (event: AlertStreamEvent) => void,
  onError?: (err: Event | Error) => void,
  token?: string,
): AlertStreamHandle {
  const base = apiBase();
  if (!base) throw new Error("NEXT_PUBLIC_API_URL is not configured");
  if (typeof fetch === "undefined" || typeof AbortController === "undefined") {
    throw new Error("fetch/AbortController are not available in this environment");
  }

  const controller = new AbortController();
  let closed = false;
  const url = `${base}/api/v1/alerts/stream`;
  const headers: Record<string, string> = { Accept: "text/event-stream" };
  if (token) headers["Authorization"] = `Bearer ${token}`;

  const dispatch = (raw: string) => {
    try {
      const data = JSON.parse(raw) as Record<string, unknown>;
      if (data.type === "connected") {
        onEvent({ type: "connected", connection_id: data.connection_id as string });
      } else if (data.type === "keepalive") {
        onEvent({ type: "keepalive" });
      } else {
        onEvent({ type: "alert", alert: data as unknown as ApiAlert });
      }
    } catch (err) {
      onError?.(err instanceof Error ? err : new Error(String(err)));
    }
  };

  (async () => {
    try {
      const res = await fetch(url, {
        method: "GET",
        headers,
        credentials: "include",
        signal: controller.signal,
      });
      if (!res.ok || !res.body) {
        throw new Error(`SSE connect failed (${res.status})`);
      }
      const reader = res.body.getReader();
      const decoder = new TextDecoder("utf-8");
      let buffer = "";
      while (!closed) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        // SSE events are separated by a blank line ("\n\n").
        let idx: number;
        while ((idx = buffer.indexOf("\n\n")) !== -1) {
          const rawEvent = buffer.slice(0, idx);
          buffer = buffer.slice(idx + 2);
          // Concatenate all `data:` lines per the SSE spec.
          const dataLines = rawEvent
            .split("\n")
            .filter((line) => line.startsWith("data:"))
            .map((line) => line.slice(5).replace(/^ /, ""));
          if (dataLines.length > 0) dispatch(dataLines.join("\n"));
        }
      }
    } catch (err) {
      if (closed) return;
      onError?.(err instanceof Error ? err : new Error(String(err)));
    }
  })();

  return {
    close: () => {
      closed = true;
      try { controller.abort(); } catch { /* ignore */ }
    },
  };
}

// ── Worker download URL ──────────────────────────────────────────────────────

/**
 * Parse a storage URI of the form `r2://<bucket>/<key>` or
 * `local://<bucket>/<key>` into its parts. Returns null if the URI is missing
 * or malformed.
 */
export function parseStorageUri(
  uri: string | null | undefined,
): { bucket: string; key: string } | null {
  if (!uri) return null;
  const m = /^(?:r2|local):\/\/([^/]+)\/(.+)$/.exec(uri);
  return m ? { bucket: m[1], key: m[2] } : null;
}

// ── Settings (Issue #112) ────────────────────────────────────────────────────

export async function getSystemSettings(
  token?: string,
): Promise<SystemSettingsResponse> {
  const base = apiBase();
  if (!base) throw new Error("NEXT_PUBLIC_API_URL is not configured");
  return getJSON<SystemSettingsResponse>(`${base}/api/v1/settings/system`, token);
}

export async function updateSystemSettings(
  patch: SystemSettingsUpdate,
  token?: string,
): Promise<SystemSettingsResponse> {
  const base = apiBase();
  if (!base) throw new Error("NEXT_PUBLIC_API_URL is not configured");
  const res = await fetch(`${base}/api/v1/settings/system`, {
    method: "PATCH",
    headers: authHeaders(token),
    body: JSON.stringify(patch),
  });
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`Settings update failed (${res.status}): ${body}`);
  }
  return res.json() as Promise<SystemSettingsResponse>;
}

export async function getUserPreferences(
  token?: string,
): Promise<UserPreferences> {
  const base = apiBase();
  if (!base) throw new Error("NEXT_PUBLIC_API_URL is not configured");
  return getJSON<UserPreferences>(`${base}/api/v1/settings/me`, token);
}

export async function updateUserPreferences(
  patch: Partial<UserPreferences>,
  token?: string,
): Promise<UserPreferences> {
  const base = apiBase();
  if (!base) throw new Error("NEXT_PUBLIC_API_URL is not configured");
  const res = await fetch(`${base}/api/v1/settings/me`, {
    method: "PATCH",
    headers: authHeaders(token),
    body: JSON.stringify(patch),
  });
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`Preferences update failed (${res.status}): ${body}`);
  }
  return res.json() as Promise<UserPreferences>;
}

// ── Reports (Issue #111) ─────────────────────────────────────────────────────

export async function listReports(token?: string): Promise<ReportJob[]> {
  const base = apiBase();
  if (!base) throw new Error("NEXT_PUBLIC_API_URL is not configured");
  return getJSON<ReportJob[]>(`${base}/api/v1/reports`, token);
}

export async function createReport(
  body: ReportCreateRequest,
  token?: string,
): Promise<ReportJob> {
  const base = apiBase();
  if (!base) throw new Error("NEXT_PUBLIC_API_URL is not configured");
  const res = await fetch(`${base}/api/v1/reports`, {
    method: "POST",
    headers: authHeaders(token),
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const errorBody = await res.text();
    throw new Error(`Report request failed (${res.status}): ${errorBody}`);
  }
  return res.json() as Promise<ReportJob>;
}

export async function getReport(
  reportId: string,
  token?: string,
): Promise<ReportJob> {
  const base = apiBase();
  if (!base) throw new Error("NEXT_PUBLIC_API_URL is not configured");
  return getJSON<ReportJob>(`${base}/api/v1/reports/${reportId}`, token);
}

export async function getReportDownloadUrl(
  reportId: string,
  token?: string,
): Promise<ReportDownloadResponse> {
  const base = apiBase();
  if (!base) throw new Error("NEXT_PUBLIC_API_URL is not configured");
  return getJSON<ReportDownloadResponse>(
    `${base}/api/v1/reports/${reportId}/download`,
    token,
  );
}

async function _fetchVideoDownloadUrlFrom(
  base: string,
  bucket: string,
  key: string,
  token?: string,
): Promise<string | null> {
  const params = new URLSearchParams({ bucket, key });
  const res = await fetch(
    `${base}/api/v1/videos/download-url?${params.toString()}`,
    { headers: getHeaders(token) },
  );
  if (!res.ok) return null;
  const body = (await res.json()) as { downloadUrl?: string };
  return body.downloadUrl ?? null;
}

/**
 * Fetch a signed download/streaming URL for an R2-backed video object.
 * The returned URL can be used directly as a `<video src>`.
 */
export async function fetchVideoDownloadUrl(
  bucket: string,
  key: string,
  token?: string,
): Promise<string | null> {
  const workerUrl = process.env.NEXT_PUBLIC_WORKER_URL;
  const apiUrl = apiBase();

  // Try the Worker first when one is explicitly configured. If the Worker
  // rejects the token (wrong JWT secret) or is unreachable, fall back to the
  // backend's identical endpoint.
  if (workerUrl) {
    try {
      const url = await _fetchVideoDownloadUrlFrom(workerUrl, bucket, key, token);
      if (url) return url;
    } catch {
      // Fall through to API.
    }
  }

  if (!apiUrl) return null;
  try {
    return await _fetchVideoDownloadUrlFrom(apiUrl, bucket, key, token);
  } catch {
    return null;
  }
}

// ── CFBD cached analytics (Issue #163) ──────────────────────────────────────
// These hit the Football-IQ backend only (NEXT_PUBLIC_API_URL). The backend
// reads cached CollegeFootballData.com rows — the CFBD key is never sent to or
// seen by the browser.

export async function fetchCfbdToledoTeam(
  token?: string,
): Promise<CfbdTeamResponse> {
  const base = apiBase();
  if (!base) throw new Error("NEXT_PUBLIC_API_URL is not configured");
  return getJSON<CfbdTeamResponse>(`${base}/api/cfbd/teams/toledo`, token);
}

export async function fetchCfbdToledoSchedule(
  season?: number,
  token?: string,
): Promise<CfbdScheduleResponse> {
  const base = apiBase();
  if (!base) throw new Error("NEXT_PUBLIC_API_URL is not configured");
  const qs = season != null ? `?season=${encodeURIComponent(season)}` : "";
  return getJSON<CfbdScheduleResponse>(
    `${base}/api/cfbd/teams/toledo/schedule${qs}`,
    token,
  );
}

export async function fetchCfbdMacBenchmark(
  season?: number,
  token?: string,
): Promise<CfbdMacBenchmarkResponse> {
  const base = apiBase();
  if (!base) throw new Error("NEXT_PUBLIC_API_URL is not configured");
  const qs = season != null ? `?season=${encodeURIComponent(season)}` : "";
  return getJSON<CfbdMacBenchmarkResponse>(
    `${base}/api/cfbd/mac/benchmark${qs}`,
    token,
  );
}
