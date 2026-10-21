/**
 * Football-IQ API client — upload pipeline, library, inbox, alerts, clips.
 *
 * Environment variables consumed (public, browser-safe):
 *   NEXT_PUBLIC_API_URL — backend base URL (FastAPI)
 */

import type {
  ApiClip,
  ApiJob,
  ApiPlayer,
  ApiPracticeSessionGroup,
  ApiVideo,
  ApiSession,
  SystemStatus,
  ProcessingProfile,
  TrainingDataset,
  ModelVersion,
  LearningBenchmark,
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
import { ApiError, apiErrorFromResponse, offlineError } from "./api-error";

// ── Helpers ──────────────────────────────────────────────────────────────────

function authHeaders(token?: string): Record<string, string> {
  const h: Record<string, string> = { "Content-Type": "application/json" };
  if (token) h["Authorization"] = `Bearer ${token}`;
  return h;
}

function getHeaders(token?: string): Record<string, string> {
  return token ? { Authorization: `Bearer ${token}` } : {};
}

/** Server hiccups worth one quiet retry for a read (restarts, dropped connections). */
const RETRYABLE_READ_STATUS = new Set([500, 502, 503, 504]);
export const READ_RETRY_DELAY_MS = 700;

/**
 * ``fetch`` for reads: retries once after a 5xx so a momentary server blip
 * never reaches the screen. A network error is not retried (the browser is
 * offline; say so at once). Never used for writes, which may already have
 * been applied.
 */
export async function fetchForRead(url: string, init?: RequestInit): Promise<Response> {
  const res = await fetch(url, init);
  if (!RETRYABLE_READ_STATUS.has(res.status)) return res;
  await new Promise((resolve) => setTimeout(resolve, READ_RETRY_DELAY_MS));
  return fetch(url, init);
}

async function getJSON<T>(url: string, token?: string): Promise<T> {
  let res: Response;
  try {
    res = await fetchForRead(url, { headers: getHeaders(token) });
  } catch (cause) {
    throw offlineError("load the latest data", cause);
  }
  if (!res.ok) throw await apiErrorFromResponse(res, "load the latest data");
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
    throw await apiErrorFromResponse(res, "start the upload");
  }
  return res.json() as Promise<UploadUrlResponse>;
}

export async function requestUploadUrl(
  filename: string,
  token?: string,
): Promise<UploadUrlResponse> {
  const apiUrl = apiBase();
  return _requestUploadUrlFrom(apiUrl, filename, token);
}

// ── Object Upload ────────────────────────────────────────────────────────────

export interface ObjectUploadResult {
  key: string;
  size: number;
  etag: string;
  storageUri: string;
}

export async function uploadToObjectStore(
  uploadUrl: string,
  file: File,
  token?: string,
  onProgress?: (loaded: number, total: number) => void,
  signal?: AbortSignal,
): Promise<ObjectUploadResult> {
  if (onProgress && typeof XMLHttpRequest !== "undefined") {
    return uploadWithXhr(uploadUrl, file, token, onProgress, signal);
  }
  const headers: Record<string, string> = {
    "Content-Type": file.type || "video/mp4",
  };
  if (token) headers["Authorization"] = `Bearer ${token}`;

  const res = await fetch(uploadUrl, {
    method: "PUT",
    headers,
    body: file,
    signal,
  });
  if (!res.ok) {
    throw await apiErrorFromResponse(res, "upload the film");
  }
  return res.json() as Promise<ObjectUploadResult>;
}

function uploadWithXhr(
  url: string,
  file: File,
  token: string | undefined,
  onProgress: (loaded: number, total: number) => void,
  signal?: AbortSignal,
): Promise<ObjectUploadResult> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) { reject(new DOMException("Upload cancelled", "AbortError")); return; }
    const xhr = new XMLHttpRequest();
    const abort = () => xhr.abort();
    signal?.addEventListener("abort", abort, { once: true });
    xhr.addEventListener("loadend", () => signal?.removeEventListener("abort", abort));
    xhr.open("PUT", url);
    xhr.setRequestHeader("Content-Type", file.type || "video/mp4");
    if (token) xhr.setRequestHeader("Authorization", `Bearer ${token}`);

    xhr.upload.addEventListener("progress", (e) => {
      if (e.lengthComputable) onProgress(e.loaded, e.total);
    });

    xhr.addEventListener("load", () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          resolve(JSON.parse(xhr.responseText) as ObjectUploadResult);
        } catch {
          reject(new ApiError({ status: 500, action: "upload the film", technical: "Invalid JSON response from upload" }));
        }
      } else {
        reject(new ApiError({ status: xhr.status, action: "upload the film", body: xhr.responseText }));
      }
    });

    xhr.addEventListener("error", () => reject(offlineError("upload the film")));
    xhr.addEventListener("abort", () => reject(new Error("Upload was cancelled.")));
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
  /** Free-form coach labels, stored in the video's metadata bag. */
  tags?: string[];
  library_role?: "toledo" | "opponent";
  camera_label?: string | null;
}

export interface SessionCreate {
  title: string;
  session_kind?: SessionKind | null;
  recorded_at?: string | null;
  opponent_team?: string | null;
  library_role?: "toledo" | "opponent";
  notes?: string | null;
}

async function mutateJSON<T>(path: string, method: "POST" | "PATCH", body: unknown, token?: string): Promise<T> {
  const response = await fetch(`${apiBase()}${path}`, { method, headers: authHeaders(token), body: JSON.stringify(body) });
  if (!response.ok) throw await apiErrorFromResponse(response, "save your changes");
  return response.json() as Promise<T>;
}

export function fetchSessions(token?: string): Promise<ApiSession[]> {
  return getJSON(`${apiBase()}/api/v1/sessions`, token);
}
export function fetchSession(id: string, token?: string): Promise<ApiSession> {
  return getJSON(`${apiBase()}/api/v1/sessions/${encodeURIComponent(id)}`, token);
}
export function createSession(body: SessionCreate, token?: string): Promise<ApiSession> {
  return mutateJSON("/api/v1/sessions", "POST", body, token);
}
export function updateSession(id: string, body: Partial<SessionCreate>, token?: string): Promise<ApiSession> {
  return mutateJSON(`/api/v1/sessions/${encodeURIComponent(id)}`, "PATCH", body, token);
}
export function addSessionRecording(id: string, body: { video_id: string; camera_label?: string | null }, token?: string): Promise<ApiSession> {
  return mutateJSON(`/api/v1/sessions/${encodeURIComponent(id)}/recordings`, "POST", body, token);
}
export function matchSessionPlays(id: string, body: { clip_ids: string[]; play_id?: string; title?: string }, token?: string): Promise<ApiSession> {
  return mutateJSON(`/api/v1/sessions/${encodeURIComponent(id)}/plays/match`, "POST", body, token);
}
export function updateVideo(id: string, body: Partial<RegisterVideoRequest>, token?: string): Promise<ApiVideo> {
  return mutateJSON(`/api/v1/videos/${encodeURIComponent(id)}`, "PATCH", body, token);
}

export function fetchSystemStatus(token?: string): Promise<SystemStatus> {
  return getJSON(`${apiBase()}/api/v1/system/status`, token);
}

export interface SessionImportPreview {
  dry_run: boolean;
  session_id: string;
  title: string;
  created: number;
  skipped: number;
  media_count: number;
  conflicts: Array<{ table: string; id: string; reason: string }>;
}
export function exportSessionArchive(id: string, token?: string): Promise<{ download_url: string; archive_id: string; size_bytes: number; sha256: string }> {
  return mutateJSON(`/api/v1/library/sessions/${encodeURIComponent(id)}/export`, "POST", {}, token);
}
export async function importSessionArchive(file: File, dryRun = true, token?: string): Promise<SessionImportPreview> {
  const body = new FormData();
  body.append("file", file);
  body.append("dry_run", String(dryRun));
  const response = await fetch(`${apiBase()}/api/v1/library/import`, { method: "POST", headers: getHeaders(token), body });
  if (!response.ok) throw await apiErrorFromResponse(response, "import the session");
  return response.json() as Promise<SessionImportPreview>;
}
export function repairSystem(action: "restart-worker" | "restart-api" | "install-detector" | "clear-cache", token?: string): Promise<{ id: string; action: string; state: "queued" }> {
  return mutateJSON("/api/v1/system/repair", "POST", { action }, token);
}

export function listTrainingDatasets(token?: string): Promise<TrainingDataset[]> {
  return getJSON(`${apiBase()}/api/v1/mlops/datasets`, token);
}
export function getTrainingDatasetDownloadUrl(id: string, token?: string): Promise<{ download_url: string }> {
  return getJSON(`${apiBase()}/api/v1/learning/datasets/${encodeURIComponent(id)}/download`, token);
}
export function listModelVersions(token?: string): Promise<ModelVersion[]> {
  return getJSON(`${apiBase()}/api/v1/mlops/models`, token);
}
export function promoteModelVersion(id: string, stage: "staging" | "production" | "retired", token?: string): Promise<ModelVersion> {
  return mutateJSON(`/api/v1/mlops/models/${encodeURIComponent(id)}/promote`, "POST", { stage }, token);
}
export function listLearningBenchmarks(token?: string): Promise<LearningBenchmark[]> {
  return getJSON(`${apiBase()}/api/v1/learning/benchmarks`, token);
}
export interface LearningSessionSplit { session_id: string; split: "train" | "calibration" | "holdout" }
export interface LearningFeaturePreview { ready: boolean; counts: Record<string, number>; required: Record<string, number>; class_counts: Record<string, unknown>; issues: string[]; job_ids: string[] }
export interface LearningFeatureManifest { id: string; name?: string; sessions?: LearningSessionSplit[]; training_dataset_id: string; features_uri: string; labels_uri: string; benchmark_id: string; counts: Record<string, number>; job_ids: string[] }
export function listFeatureManifests(token?: string): Promise<LearningFeatureManifest[]> {
  return getJSON(`${apiBase()}/api/v1/learning/feature-manifests`, token);
}
export function previewFeatureManifest(body: { name?: string; sessions: LearningSessionSplit[] }, token?: string): Promise<LearningFeaturePreview> {
  return mutateJSON("/api/v1/learning/feature-manifests/preview", "POST", body, token);
}
export function createFeatureManifest(body: { name?: string; sessions: LearningSessionSplit[] }, token?: string): Promise<LearningFeatureManifest> {
  return mutateJSON("/api/v1/learning/feature-manifests", "POST", body, token);
}
export function queueCoverageTraining(body: { training_dataset_id: string; features_uri: string }, token?: string): Promise<{ job_id: string; status: string }> {
  return mutateJSON("/api/v1/learning/train", "POST", { ...body, training_variant: "local_coverage" }, token);
}
export function listLearningCandidates(manifestId: string, token?: string): Promise<ModelVersion[]> {
  return getJSON(`${apiBase()}/api/v1/learning/candidates?${new URLSearchParams({ manifest_id: manifestId })}`, token);
}
export function evaluateLearningCandidate(modelId: string, benchmarkId: string, token?: string): Promise<{ passed: boolean; accuracy: number; macro_f1: number; sample_count: number; prediction_count: number }> {
  return mutateJSON(`/api/v1/learning/candidates/${encodeURIComponent(modelId)}/evaluate`, "POST", { benchmark_id: benchmarkId }, token);
}
export function exportCorrections(modelScope: string, token?: string): Promise<{ exported_count: number; label_ids: string[]; training_dataset_id: string | null }> {
  return mutateJSON(`/api/v1/corrections/export?${new URLSearchParams({ model_scope: modelScope })}`, "POST", {}, token);
}

export interface VideoRegistrationResult extends ApiVideo {
  /** The server reused identical film and preserved its existing session and tags. */
  duplicate?: boolean;
}

export async function registerVideo(
  data: RegisterVideoRequest,
  token?: string,
): Promise<VideoRegistrationResult> {
  const base = apiBase();
  const res = await fetch(`${base}/api/v1/videos`, {
    method: "POST",
    headers: authHeaders(token),
    body: JSON.stringify(data),
  });
  if (!res.ok) {
    throw await apiErrorFromResponse(res, "add the film to the library");
  }
  return res.json() as Promise<VideoRegistrationResult>;
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
  // Jobs waiting for a worker to claim them — the server-side truth that
  // "Process Film" was requested. Optional so older backends/mocks stay valid.
  queued_jobs?: number;
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
  const params = new URLSearchParams();
  if (includeSucceeded) params.set("include_succeeded", "true");
  const qs = params.toString();
  const url = `${base}/api/v1/inbox/status${qs ? `?${qs}` : ""}`;
  const res = await fetchForRead(url, { headers: authHeaders(token) });
  if (!res.ok) {
    throw await apiErrorFromResponse(res, "check processing status");
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
  library_role?: "toledo" | "opponent";
}

export async function fetchVideos(
  filters: VideoFilters = {},
  token?: string,
): Promise<ApiVideo[]> {
  const base = apiBase();
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
  return getJSON<ApiVideo>(`${base}/api/v1/videos/${videoId}`, token);
}

export interface JobFilters {
  status?: string;
  job_type?: string;
  video_id?: string;
  has_video?: boolean;
  limit?: number;
  offset?: number;
}

export async function fetchJobs(filters: JobFilters = {}, token?: string): Promise<ApiJob[]> {
  const base = apiBase();
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(filters)) {
    if (v !== undefined && v !== null && v !== "") params.set(k, String(v));
  }
  const qs = params.toString();
  return getJSON<ApiJob[]>(`${base}/api/v1/jobs${qs ? `?${qs}` : ""}`, token);
}

export async function fetchAllJobs(filters: JobFilters = {}, token?: string): Promise<ApiJob[]> {
  const jobs: ApiJob[] = [];
  const limit = 200;
  for (let offset = 0; ; offset += limit) {
    const page = await fetchJobs({ ...filters, limit, offset }, token);
    jobs.push(...page);
    if (page.length < limit) return jobs;
  }
}

export async function fetchClipsForVideo(
  videoId: string,
  token?: string,
): Promise<ApiClip[]> {
  const base = apiBase();
  const clips: ApiClip[] = [];
  const limit = 500;
  for (let offset = 0; ; offset += limit) {
    const page = await getJSON<ApiClip[]>(
      `${base}/api/v1/videos/${videoId}/clips?limit=${limit}&offset=${offset}`,
      token,
    );
    clips.push(...page);
    if (page.length < limit) return clips;
  }
}

export async function fetchClip(clipId: string, token?: string): Promise<ApiClip> {
  const base = apiBase();
  return getJSON<ApiClip>(`${base}/api/v1/clips/${clipId}`, token);
}

// ── Clip overlays (Issue #104) ───────────────────────────────────────────────

export async function fetchClipOverlays(
  clipId: string,
  token?: string,
): Promise<ClipOverlayPayload> {
  const base = apiBase();
  const payload = await getJSON<ClipOverlayPayload>(
    `${base}/api/v1/clips/${clipId}/overlays`,
    token,
  );
  if (payload.tracks_timebase !== "recording" || typeof payload.source_start_time_seconds !== "number" || !(payload.source_fps && payload.source_fps > 0)) return payload;
  const offset = payload.source_start_time_seconds;
  const frameOffset = Math.round(offset * payload.source_fps);
  return {
    ...payload,
    tracks_timebase: "clip",
    tracklets: payload.tracklets.map((track) => ({ ...track, start_frame: track.start_frame - frameOffset, end_frame: track.end_frame - frameOffset, track_points: track.track_points.map((point) => ({ ...point, frame_number: point.frame_number - frameOffset, timestamp_seconds: point.timestamp_seconds == null ? point.timestamp_seconds : point.timestamp_seconds - offset })) })),
    events: payload.events.map((event) => event.timebase !== "recording" ? event : { ...event, timebase: "clip", frame_number: event.frame_number == null ? null : event.frame_number - frameOffset, timestamp_seconds: event.timestamp_seconds == null ? null : event.timestamp_seconds - offset }),
    warnings: [...(payload.warnings ?? []), ...(payload.source_fps_known === false ? ["Recording frame rate is unavailable. Tracking alignment uses an estimated frame rate."] : [])],
  };
}

// ── Self-Scout + Opponent Scout (Issue #105) ─────────────────────────────────

export async function fetchSelfScoutTendencies(
  videoId?: string | null,
  token?: string,
  filters?: number | { limit?: number; recorded_after?: string; recorded_before?: string; session_kind?: SessionKind; practice_session_id?: string; session_id?: string },
): Promise<SelfScoutResponse> {
  const base = apiBase();
  const params = new URLSearchParams();
  if (videoId) params.set("video_id", videoId);
  if (typeof filters === "number") params.set("limit", String(filters));
  else if (filters) for (const [key, value] of Object.entries(filters)) if (value != null) params.set(key === "practice_session_id" ? "session_id" : key, String(value));
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
  const params = new URLSearchParams({ opponent_video_id: opponentVideoId });
  if (limit != null) params.set("limit", String(limit));
  return getJSON<SelfScoutResponse>(
    `${base}/api/v1/self-scout/opponent-tendencies?${params.toString()}`,
    token,
  );
}

export async function fetchOpponents(token?: string): Promise<OpponentSummary[]> {
  const base = apiBase();
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

export interface PlayerCreate {
  id?: string;
  first_name: string;
  last_name: string;
  jersey_number?: number | null;
  position?: string | null;
  position_group?: "QB" | "Skill" | "OL" | "DL" | "LB" | "DB" | "ST" | null;
  is_active?: boolean;
}
export interface PlayerImportResult {
  dry_run: boolean;
  created: number;
  updated: number;
  skipped: number;
  rows: Array<{ line: number; status: "create" | "existing" | "conflict" | "invalid"; player?: ApiPlayer; error?: string }>;
  errors: Array<{ line: number; error: string }>;
}
export function createPlayer(body: PlayerCreate, token?: string): Promise<ApiPlayer> {
  return mutateJSON("/api/v1/players", "POST", body, token);
}
export function updatePlayer(id: string, body: Partial<Omit<PlayerCreate, "id">>, token?: string): Promise<ApiPlayer> {
  return mutateJSON(`/api/v1/players/${encodeURIComponent(id)}`, "PATCH", body, token);
}
export function importPlayers(csv: string, dryRun = true, token?: string): Promise<PlayerImportResult> {
  return mutateJSON("/api/v1/players/import", "POST", { csv, dry_run: dryRun }, token);
}

export async function fetchPlayers(
  filters: PlayerFilters = {},
  token?: string,
): Promise<ApiPlayer[]> {
  const base = apiBase();
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
  return getJSON<ApiPlayer>(`${base}/api/v1/players/${playerId}`, token);
}

/** One player's tracking aggregates from GET /api/v1/players/metrics/summary. */
export interface ApiPlayerMetricsSummary {
  player_id: string;
  tracklet_count: number;
  tracked_clip_count: number;
  last_tracked_at: string | null;
  tracking_confidence: number | null;
  identity_bucket: "known" | "probable" | "needs_review";
  max_speed_yps: number | null;
  max_speed_samples: number;
  distance_yards: number | null;
  distance_samples: number;
}

export interface ApiPlayerMetricsWeekly {
  week_start: string;
  tracked_clip_count: number;
  tracking_confidence: number | null;
  max_speed_yps: number | null;
  distance_yards: number | null;
}

export interface ApiPlayerMetricsDetail {
  summary: ApiPlayerMetricsSummary;
  weekly: ApiPlayerMetricsWeekly[];
}

/**
 * Batched aggregates for every player with tracked film. Players without
 * attributed tracklets are absent — callers keep their honest empty state.
 */
export async function fetchPlayerMetricsSummaries(
  token?: string,
): Promise<ApiPlayerMetricsSummary[]> {
  const base = apiBase();
  return getJSON<ApiPlayerMetricsSummary[]>(`${base}/api/v1/players/metrics/summary`, token);
}

export async function fetchPlayerMetricsDetail(
  playerId: string,
  token?: string,
): Promise<ApiPlayerMetricsDetail> {
  const base = apiBase();
  return getJSON<ApiPlayerMetricsDetail>(
    `${base}/api/v1/players/${playerId}/metrics/summary`,
    token,
  );
}

// ── Game summary (film + CFBD) ───────────────────────────────────────────────

export interface ApiPlayGroup {
  key: string;
  label: string;
  plays: number;
  clip_ids: string[];
}

export interface ApiDownSummary {
  down: number;
  plays: number;
  success_rate: number | null;
  yards_per_play: number | null;
  rush_share: number | null;
  clip_ids: string[];
}

export interface ApiPlayDetail {
  clip_id: string;
  play_number: number | null;
  period: number | null;
  down: number | null;
  distance: number | null;
  yards_gained: number;
  kind: "rush" | "pass" | "other";
  epa: number | null;
  success: boolean | null;
  explosive: boolean;
  turnover: boolean;
  scoring: boolean;
  text: string | null;
}

export interface ApiSideSummary {
  plays: number;
  yards: number;
  yards_per_play: number | null;
  success_rate: number | null;
  epa_per_play: number | null;
  rush_plays: number;
  rush_yards: number;
  rush_yards_per_play: number | null;
  rush_success_rate: number | null;
  pass_plays: number;
  pass_yards: number;
  pass_yards_per_play: number | null;
  pass_success_rate: number | null;
  third_down_attempts: number;
  third_down_conversions: number;
  explosive_plays: number;
  turnovers: number;
  by_down: ApiDownSummary[];
  groups: ApiPlayGroup[];
  /** Every counted play in game order (older API builds omit it). */
  play_log?: ApiPlayDetail[];
}

export interface ApiGameSummary {
  video_id: string;
  opponent: string | null;
  film_plays: number;
  linked_plays: number;
  offense: ApiSideSummary | null;
  defense: ApiSideSummary | null;
}

export async function fetchGameSummary(videoId: string, token?: string): Promise<ApiGameSummary> {
  return getJSON<ApiGameSummary>(
    `${apiBase()}/api/v1/videos/${encodeURIComponent(videoId)}/game-summary`,
    token,
  );
}

// ── Team dashboard (Home) ───────────────────────────────────────────────────

export interface ApiTeamWeek {
  week_start: string;
  session_kind: "practice" | "game" | string;
  reps: number;
  players: number;
  yards_per_rep: number | null;
  high_speed_yards_per_rep: number | null;
  sprints_per_rep: number | null;
  top_speed_p90_yps: number | null;
  get_off_yards: number | null;
}

export interface ApiGroupWeek {
  week_start: string;
  reps: number;
  top_speed_p90_yps: number | null;
  high_speed_yards_per_rep: number | null;
  get_off_yards: number | null;
}

export interface ApiPositionGroupTrend {
  position_group: string;
  weekly: ApiGroupWeek[];
  change: Record<string, number | null>;
}

export interface ApiGameSideSummary {
  plays: number;
  yards_per_play: number | null;
  success_rate: number | null;
  epa_per_play: number | null;
  third_down_rate: number | null;
  explosive_plays: number;
  turnovers: number;
}

export interface ApiDashboardGame {
  video_id: string;
  date: string | null;
  opponent: string | null;
  linked_plays: number;
  offense: ApiGameSideSummary | null;
  defense: ApiGameSideSummary | null;
}

export interface ApiReadinessGroup {
  position_group: string;
  player_count: number;
  mean_acwr: number | null;
  mean_injury_risk_score: number | null;
  max_injury_risk_score: number | null;
}

export interface ApiFlaggedPlayer {
  player_id: string;
  name: string;
  jersey_number: number | null;
  position_group: string | null;
  injury_risk_score: number;
  acwr: number | null;
}

export interface ApiTeamDashboard {
  weekly: ApiTeamWeek[];
  position_groups: ApiPositionGroupTrend[];
  games: ApiDashboardGame[];
  readiness: {
    visible: boolean;
    player_level: boolean;
    as_of: string | null;
    groups: ApiReadinessGroup[];
    flagged: ApiFlaggedPlayer[];
  };
  activity: {
    sessions_this_week: number;
    plays_this_week: number;
    film_processing: number;
    film_uploaded: number;
  };
}

export async function fetchTeamDashboard(
  options: { weeks: number; sessionKind?: "practice" | "game" | null },
  token?: string,
): Promise<ApiTeamDashboard> {
  const params = new URLSearchParams({ weeks: String(options.weeks) });
  if (options.sessionKind) params.set("session_kind", options.sessionKind);
  return getJSON<ApiTeamDashboard>(`${apiBase()}/api/v1/team/dashboard?${params.toString()}`, token);
}

// ── Physical load ────────────────────────────────────────────────────────────

/** Physical load summed over a player's tracked reps (``physical_profile``). */
export interface ApiPhysicalLoad {
  player_id: string;
  reps: number;
  seconds: number;
  yards: number;
  high_speed_yards: number;
  sprint_yards: number;
  sprints: number;
  high_speed_efforts: number;
  accelerations: number;
  decelerations: number;
  top_speed_yps: number | null;
  peak_accel_yps2: number | null;
  peak_decel_yps2: number | null;
  get_off_yards: number | null;
  get_off_reps: number;
  band_yards: Record<string, number>;
}

export interface ApiPhysicalSession {
  day: string;
  reps: number;
  yards: number;
  high_speed_yards: number;
  sprints: number;
  top_speed_yps: number | null;
}

export interface ApiPlayerPhysicalDetail {
  load: ApiPhysicalLoad | null;
  sessions: ApiPhysicalSession[];
}

export async function fetchPlayerPhysical(
  playerId: string,
  token?: string,
): Promise<ApiPlayerPhysicalDetail> {
  return getJSON<ApiPlayerPhysicalDetail>(
    `${apiBase()}/api/v1/players/${encodeURIComponent(playerId)}/metrics/physical`,
    token,
  );
}

export async function fetchPhysicalLoads(token?: string): Promise<ApiPhysicalLoad[]> {
  return getJSON<ApiPhysicalLoad[]>(`${apiBase()}/api/v1/players/metrics/physical`, token);
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
  client_operation_id?: string;
  expected_revision?: number;
}

export interface ClipUpdate {
  is_reviewed?: boolean;
  expected_revision?: number;
  personnel_grouping?: string | null;
  down?: number | null;
  distance?: number | null;
  field_zone?: string | null;
  our_possession?: OurPossession | null;
  label_data?: Record<string, unknown> | null;
  tracking_review_required?: boolean;
}

/** Persist coaching metadata and review status with optimistic concurrency. */
export async function updateClip(clipId: string, patch: ClipUpdate, token?: string): Promise<ApiClip> {
  const res = await fetch(`${apiBase()}/api/v1/clips/${encodeURIComponent(clipId)}`, {
    method: "PATCH", headers: authHeaders(token), body: JSON.stringify(patch),
  });
  if (!res.ok) throw await apiErrorFromResponse(res, "save the play");
  return res.json() as Promise<ApiClip>;
}

export interface CorrectionResponse extends CorrectionCreate {
  id: string;
  corrected_by: string;
  exported_as_label: boolean;
  created_at: string;
  original_value: Record<string, unknown> | null;
  notes: string | null;
  training_eligible: boolean;
  annotation_revision?: number | null;
}

export function fetchCorrections(clipId: string, token?: string): Promise<CorrectionResponse[]> {
  return getJSON(`${apiBase()}/api/v1/corrections?${new URLSearchParams({ clip_id: clipId, limit: "50" })}`, token);
}

export async function createCorrection(
  body: CorrectionCreate,
  token?: string,
): Promise<CorrectionResponse> {
  const base = apiBase();
  const res = await fetch(`${base}/api/v1/corrections`, {
    method: "POST",
    headers: authHeaders(token),
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    throw await apiErrorFromResponse(res, "save the correction");
  }
  return res.json() as Promise<CorrectionResponse>;
}

// ── Jobs ─────────────────────────────────────────────────────────────────────

export async function retryJob(jobId: string, token?: string): Promise<unknown> {
  const base = apiBase();
  const res = await fetch(`${base}/api/v1/jobs/${jobId}/retry`, {
    method: "POST",
    headers: authHeaders(token),
  });
  if (!res.ok) {
    throw await apiErrorFromResponse(res, "retry processing");
  }
  return res.json();
}

export function controlJob(id: string, action: "pause" | "resume" | "cancel", token?: string): Promise<ApiJob> {
  return mutateJSON(`/api/v1/jobs/${encodeURIComponent(id)}/${action}`, "POST", {}, token);
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
 * API or the upload flow, and it never calls an external API. Defaults to
 * nightly (full-quality) priority; ``same_session`` (priority 10) is the
 * period-break fast path.
 */
export async function requestVideoProcessing(
  videoId: string,
  options?: { mode?: ProcessingMode; profile?: ProcessingProfile },
  token?: string,
): Promise<ProcessVideoResult> {
  const base = apiBase();
  const mode: ProcessingMode = options?.mode ?? (options?.profile === "quick" || options?.profile === "review" ? "same_session" : "nightly");
  const priority = mode === "same_session" ? 10 : 0;
  const res = await fetch(`${base}/api/v1/jobs`, {
    method: "POST",
    headers: authHeaders(token),
    body: JSON.stringify({
      video_id: videoId,
      job_type: "pipeline",
      priority,
      pipeline_mode: mode,
      ...(options?.profile ? { processing_profile: options.profile } : {}),
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
    throw await apiErrorFromResponse(res, "start processing");
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
  const res = await fetch(`${base}/api/v1/alerts/${alertId}/action`, {
    method: "PATCH",
    headers: authHeaders(token),
  });
  if (!res.ok) {
    throw await apiErrorFromResponse(res, "update the alert");
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
    throw await apiErrorFromResponse(res, "check for tendency breaks");
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
        throw new ApiError({ status: res.status, action: "connect to live alerts" });
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

// ── Signed download URL ──────────────────────────────────────────────────────

/**
 * Parse a storage URI of the form `s3://<bucket>/<key>` or
 * `local://<bucket>/<key>` into its parts. Returns null if the URI is missing
 * or malformed.
 */
export function parseStorageUri(
  uri: string | null | undefined,
): { bucket: string; key: string } | null {
  if (!uri) return null;
  const m = /^(?:s3|local):\/\/([^/]+)\/(.+)$/.exec(uri);
  return m ? { bucket: m[1], key: m[2] } : null;
}

// ── Settings (Issue #112) ────────────────────────────────────────────────────

export async function getSystemSettings(
  token?: string,
): Promise<SystemSettingsResponse> {
  const base = apiBase();
  return getJSON<SystemSettingsResponse>(`${base}/api/v1/settings/system`, token);
}

export async function updateSystemSettings(
  patch: SystemSettingsUpdate,
  token?: string,
): Promise<SystemSettingsResponse> {
  const base = apiBase();
  const res = await fetch(`${base}/api/v1/settings/system`, {
    method: "PATCH",
    headers: authHeaders(token),
    body: JSON.stringify(patch),
  });
  if (!res.ok) {
    throw await apiErrorFromResponse(res, "save settings");
  }
  return res.json() as Promise<SystemSettingsResponse>;
}

export async function getUserPreferences(
  token?: string,
): Promise<UserPreferences> {
  const base = apiBase();
  return getJSON<UserPreferences>(`${base}/api/v1/settings/me`, token);
}

export async function updateUserPreferences(
  patch: Partial<UserPreferences>,
  token?: string,
): Promise<UserPreferences> {
  const base = apiBase();
  const res = await fetch(`${base}/api/v1/settings/me`, {
    method: "PATCH",
    headers: authHeaders(token),
    body: JSON.stringify(patch),
  });
  if (!res.ok) {
    throw await apiErrorFromResponse(res, "save your preferences");
  }
  return res.json() as Promise<UserPreferences>;
}

// ── Reports (Issue #111) ─────────────────────────────────────────────────────

export async function listReports(token?: string): Promise<ReportJob[]> {
  const base = apiBase();
  return getJSON<ReportJob[]>(`${base}/api/v1/reports`, token);
}

export async function createReport(
  body: ReportCreateRequest,
  token?: string,
): Promise<ReportJob> {
  const base = apiBase();
  const res = await fetch(`${base}/api/v1/reports`, {
    method: "POST",
    headers: authHeaders(token),
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    throw await apiErrorFromResponse(res, "create the report");
  }
  return res.json() as Promise<ReportJob>;
}

export async function getReport(
  reportId: string,
  token?: string,
): Promise<ReportJob> {
  const base = apiBase();
  return getJSON<ReportJob>(`${base}/api/v1/reports/${reportId}`, token);
}

export async function getReportDownloadUrl(
  reportId: string,
  token?: string,
): Promise<ReportDownloadResponse> {
  const base = apiBase();
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
 * Fetch a signed download/streaming URL for an object-store-backed video object.
 * The returned URL can be used directly as a `<video src>`.
 */
/** Reuse signed URLs only until shortly before their actual expiration. */
const SIGNED_URL_REFRESH_AHEAD_MS = 60 * 1000;
const SIGNED_URL_RETRY_MS = 60 * 1000;
const signedUrlCache = new Map<string, { url: string; expiresAt: number }>();

function signedUrlExpiresAt(value: string): number | null {
  try {
    const url = new URL(value, typeof window === "undefined" ? "http://localhost" : window.location.origin);
    const localExpiry = Number(url.searchParams.get("exp"));
    if (Number.isFinite(localExpiry) && localExpiry > 0) return localExpiry * 1000;

    const date = url.searchParams.get("X-Amz-Date");
    const lifetime = Number(url.searchParams.get("X-Amz-Expires"));
    const match = date?.match(/^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z$/);
    if (!match || !Number.isFinite(lifetime) || lifetime <= 0) return null;
    const issuedAt = Date.UTC(
      Number(match[1]), Number(match[2]) - 1, Number(match[3]),
      Number(match[4]), Number(match[5]), Number(match[6]),
    );
    return issuedAt + lifetime * 1000;
  } catch {
    return null;
  }
}

function signedUrlRefreshAhead(expiresAt: number, now: number): number {
  return Math.min(SIGNED_URL_REFRESH_AHEAD_MS, Math.max(0, (expiresAt - now) / 10));
}

export async function fetchVideoDownloadUrl(
  bucket: string,
  key: string,
  token?: string,
  { fresh = false }: { fresh?: boolean } = {},
): Promise<string | null> {
  const cacheKey = `${bucket}/${key}`;
  const now = Date.now();
  const cached = signedUrlCache.get(cacheKey);
  if (!fresh && cached && now < cached.expiresAt - signedUrlRefreshAhead(cached.expiresAt, now)) return cached.url;
  const apiUrl = apiBase();
  try {
    const url = await _fetchVideoDownloadUrlFrom(apiUrl, bucket, key, token);
    if (url) {
      const expiresAt = signedUrlExpiresAt(url);
      if (expiresAt != null && expiresAt > Date.now()) signedUrlCache.set(cacheKey, { url, expiresAt });
      else signedUrlCache.delete(cacheKey);
    }
    return url;
  } catch {
    return null;
  }
}

/** Milliseconds until a URL from :func:`fetchVideoDownloadUrl` should be re-minted. */
export function signedUrlRefreshDelay(bucket: string, key: string): number {
  const cached = signedUrlCache.get(`${bucket}/${key}`);
  if (!cached) return SIGNED_URL_RETRY_MS;
  const now = Date.now();
  return Math.max(0, cached.expiresAt - now - signedUrlRefreshAhead(cached.expiresAt, now));
}

// ── CFBD cached analytics (Issue #163) ──────────────────────────────────────
// These hit the Football-IQ backend only (NEXT_PUBLIC_API_URL). The backend
// reads cached CollegeFootballData.com rows — the CFBD key is never sent to or
// seen by the browser.

export async function fetchCfbdToledoTeam(
  token?: string,
): Promise<CfbdTeamResponse> {
  const base = apiBase();
  return getJSON<CfbdTeamResponse>(`${base}/api/cfbd/teams/toledo`, token);
}

export async function fetchCfbdToledoSchedule(
  season?: number,
  token?: string,
): Promise<CfbdScheduleResponse> {
  const base = apiBase();
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
  const qs = season != null ? `?season=${encodeURIComponent(season)}` : "";
  return getJSON<CfbdMacBenchmarkResponse>(
    `${base}/api/cfbd/mac/benchmark${qs}`,
    token,
  );
}
