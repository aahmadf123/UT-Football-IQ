export type PageKey =
  // Consolidated top-level destinations (ADR 0003)
  | "dashboard"
  | "film-room"
  | "scouting"
  | "players"
  | "analytics"
  | "player-development"
  | "health-workload"
  | "reports"
  | "settings"
  // Inbox + deep-link / compatibility surfaces
  | "alerts"
  | "clip-review"
  // Retained compatibility routes (no longer in primary nav — see ADR 0003)
  | "library"
  | "video-and-plays"
  | "self-scout"
  | "opponent-scout"
  | "clips-highlights"
  | "college-data";

// Backend-aligned enums (ADR 0001). These describe API payloads, not the
// existing UI filter literals in app-state.tsx — the ADR explicitly defers
// the UI-side `"special"` → `"special_teams"` rename.
export type SessionKind = "practice" | "scrimmage" | "game";
export type SourceType = "drone" | "uploaded_clip";
export type OurPossession = "offense" | "defense" | "special_teams";
export type ApiSideOfBall = "offense" | "defense" | "special_teams";

// Source-capture regime inferred from pixels at ingest (Issue #126 / ADR
// 0005). ``unknown`` is reserved for hard analysis failures and rows that
// predate the column.
export type CaptureRegime =
  | "drone_follow"
  | "fixed_sideline"
  | "unconstrained"
  | "unknown";

export interface ApiVideo {
  id: string;
  filename: string;
  status: string;
  duration_seconds?: number | null;
  fps?: number | null;
  width?: number | null;
  height?: number | null;
  created_at: string;
  recorded_at?: string | null;
  session_kind?: SessionKind | null;
  source_type?: SourceType | null;
  opponent_team?: string | null;
  practice_session_id?: string | null;
  our_possession?: OurPossession | null;
  storage_uri?: string | null;
}

// Same-session result tier + derived coach-facing review state (Issue #147).
// ``preliminary`` = same-session first pass awaiting nightly upgrade; ``final``
// = nightly full-quality output. The review state distinguishes a clip a coach
// still has to look at from one flagged low-confidence or already reviewed.
export type ClipResultState = "preliminary" | "final";
export type ClipReviewState = "reviewed" | "low_confidence" | "needs_review";

export interface ApiClip {
  id: string;
  video_id: string;
  start_time: number;
  end_time: number;
  play_number?: number | null;
  confidence?: number | null;
  is_reviewed: boolean;
  storage_uri?: string | null;
  label_data?: Record<string, unknown> | null;
  boundary_source?: string | null;
  boundary_confidence?: number | null;
  session_kind?: SessionKind | null;
  our_possession?: OurPossession | null;
  side_of_ball?: ApiSideOfBall | null;
  // Issue #147 — present from backends that expose the same-session result tier;
  // optional so older payloads (and mocks) stay valid.
  result_state?: ClipResultState | null;
  is_preliminary?: boolean;
  review_state?: ClipReviewState;
  // Capture regime the ingest detector picked for this clip's footage.
  // Optional so older payloads (and mocks) stay valid.
  capture_regime?: CaptureRegime | null;
  created_at: string;
}

export interface ApiPlayer {
  id: string;
  first_name: string;
  last_name: string;
  jersey_number: number | null;
  position: string | null;
  position_group: string | null;
  is_active: boolean;
  user_id: string | null;
  metadata: Record<string, unknown> | null;
  created_at: string;
  // Outward-facing visibility lifecycle (Issue #114). Always present in the
  // ``staff`` projection; absent in ``recruiting`` payloads.
  visibility_state?: PlayerVisibilityState;
  // Shaping mode the backend applied to this payload. Frontend uses this to
  // decide which UI panels to render — never to *enforce* access; enforcement
  // lives entirely on the backend.
  view?: VisibilityMode;
}

// Backend-aligned governance enums (Issues #113 / #114). See
// ``docs/governance.md`` for the contract.
export type PlayerVisibilityState =
  | "staff_only"
  | "player_approved"
  | "recruiting_approved"
  | "archived";

export type VisibilityMode = "staff" | "player" | "recruiting";

// Shape of the ``detail`` payload returned with HTTP 503 ``workload_gated``
// responses from heavy endpoints (POST /api/v1/jobs and similar). Surfaced so
// callers can render an actionable "system is busy" message instead of a
// generic error.
export interface WorkloadGatedDetail {
  error_code: "workload_gated";
  endpoint: string;
  message: string;
  workload: {
    queued: number;
    running: number;
    queue_threshold: number;
    running_threshold: number;
    status: "healthy" | "degraded" | "saturated";
    gating_disabled: boolean;
  };
}

export interface OpponentVideo {
  video_id: string;
  filename: string;
  status: string;
  recorded_at?: string | null;
  created_at: string;
}

export interface OpponentSummary {
  opponent_team: string;
  video_count: number;
  latest_recorded_at?: string | null;
  videos: OpponentVideo[];
}

export interface ApiPracticeSessionGroup {
  practice_session_id?: string | null;
  session_date?: string | null;
  session_kind?: SessionKind | null;
  opponent_team?: string | null;
  video_count: number;
  first_recorded_at?: string | null;
  last_recorded_at?: string | null;
}

// One entry in a job's per-stage progress map. The orchestrator heartbeats
// ``{stage[:clipprefix]: {status, ...headline numbers}}`` where status is one
// of "started" | "succeeded" | "failed" | "skipped".
export interface JobStageProgress {
  status?: string;
  [key: string]: unknown;
}

export interface ApiJob {
  id: string;
  job_type: string;
  status: string;
  priority: number;
  pipeline_mode?: string | null;
  is_same_session?: boolean;
  error_stage?: string | null;
  error_message?: string | null;
  nightly_followup_job_id?: string | null;
  // Per-stage progress map maintained by the orchestrator via heartbeat.
  progress?: Record<string, JobStageProgress> | null;
  // Lease bookkeeping from the job queue (backend JobResponse): how many
  // times a worker has claimed this job and which worker holds the current
  // lease. Optional so older payloads (and mocks) stay valid.
  attempt_count?: number;
  leased_by?: string | null;
  created_at: string;
}

export interface SelfScoutResponse {
  formation_tendencies: TendencyEntry[];
  motion_tendencies: {
    with_motion: MotionSplit;
    without_motion: MotionSplit;
  };
  field_zone_tendencies: TendencyEntry[];
  personnel_tendencies: TendencyEntry[];
  down_distance_tendencies: Array<TendencyEntry & { down: number; distance_bucket: string }>;
  formation_concept_families: Record<string, ConceptFamilyEntry[]>;
  pre_snap_tells: ExposureAlert[];
  alerts: TendencyAlert[];
  clip_count: number;
}

export interface TendencyEntry {
  grouping_key: string;
  total_plays: number;
  run_count: number;
  pass_count: number;
  run_rate: number;
  pass_rate: number;
  evidence_clip_ids: string[];
  low_sample: boolean;
}

export interface MotionSplit {
  total: number;
  run_count: number;
  pass_count: number;
  run_rate: number;
  pass_rate: number;
}

export interface ConceptFamilyEntry {
  formation: string;
  concept_family: string;
  total_plays: number;
  rate: number;
  evidence_clip_ids: string[];
  low_sample: boolean;
}

export interface ExposureAlert {
  grouping_key: string;
  formation: string;
  motion_state: string;
  total_plays: number;
  lean: string;
  severity: string;
  run_rate: number;
  pass_rate: number;
  evidence_clip_ids: string[];
  low_sample: boolean;
  message: string;
}

export interface TendencyAlert {
  alert_type: string;
  message: string;
  severity: string;
  grouping_key: string;
  run_rate: number;
  pass_rate: number;
}

export interface FootballData {
  videos: ApiVideo[];
  jobs: ApiJob[];
  selfScout: SelfScoutResponse;
  players: PlayerSummary[];
  plays: PlaySummary[];
  clips: ClipSummary[];
  health: HealthSummary[];
  alerts: AlertSummary[];
}

export interface PlayerSummary {
  id: string;
  jersey: string;
  name: string;
  position: string;
  group: string;
  // Performance + identity-confidence metrics. Optional because the live
  // `/api/v1/players` surface only returns identity in P1 — analytics overlays
  // (#100) land in later batches. UI sites render "—" when undefined rather
  // than fabricating values.
  maxSpeed?: number;
  distance?: number;
  separation?: number;
  confidence?: number;
  trend?: number[];
}

export interface PlaySummary {
  number: number;
  formation: string;
  personnel: string;
  concept: string;
  result: string;
  yards: number;
  confidence: number;
}

export interface ClipSummary {
  id: string;
  title: string;
  subtitle: string;
  duration: string;
  tag: string;
}

export interface HealthSummary {
  player: string;
  load: string;
  status: "Low" | "Med" | "High";
}

export interface AlertSummary {
  title: string;
  detail: string;
  severity: "good" | "warning" | "danger" | "info";
}

// ── Clip-review overlay payload (Issue #104) ────────────────────────────────
// These types mirror the FastAPI `/api/v1/clips/{clip_id}/overlays` schema.
// They are the read-only surface the Clip Review canvas consumes; write paths
// for the underlying tables (tracklets, labels, events, metrics) live in their
// own per-resource endpoints.

export interface OverlayTrackPoint {
  frame_number: number;
  field_x: number | null;
  field_y: number | null;
  bbox: [number, number, number, number] | null;
  detection_confidence: number | null;
}

export interface OverlayTracklet {
  id: string;
  player_id: string | null;
  start_frame: number;
  end_frame: number;
  track_confidence: number | null;
  team_label: string | null;
  position_group: string | null;
  side_of_ball: string | null;
  track_points: OverlayTrackPoint[];
}

export interface OverlayEvent {
  id: string;
  event_type: string;
  frame_number: number | null;
  timestamp_seconds: number | null;
  attributes: Record<string, unknown> | null;
}

export interface OverlayLabel {
  id: string;
  tracklet_id: string | null;
  label_type: string;
  label_value: Record<string, unknown>;
  source: string;
}

export interface OverlayMetric {
  id: string;
  tracklet_id: string | null;
  metric_name: string;
  metric_value: Record<string, unknown>;
  unit: string | null;
  confidence: number | null;
  effort_zscore?: number | null;
  loaf_flag?: boolean | null;
}

export interface OverlayLayersAvailable {
  tracklets: boolean;
  events: boolean;
  labels: boolean;
  metrics: boolean;
}

// Field-calibration state for the clip's parent video. ``reason`` is a
// coach-readable sentence composed server-side whenever spatial metrics are
// suppressed — suppression is never silent.
export interface OverlayCalibration {
  analytics_safe: boolean;
  reason: string | null;
  reason_codes: string[];
  confidence: number | null;
}

export interface ClipOverlayPayload {
  clip_id: string;
  tracklets: OverlayTracklet[];
  events: OverlayEvent[];
  labels: OverlayLabel[];
  metrics: OverlayMetric[];
  layers_available: OverlayLayersAvailable;
  // Present from backends that expose calibration-aware overlays (explained
  // suppression); optional so older payloads (and mocks) stay valid.
  capture_regime?: CaptureRegime | null;
  calibration?: OverlayCalibration;
}

// Layer keys the Clip Review UI exposes as toggles. ``raw`` is the bare video
// with all overlays hidden; ``wireframe`` is the field outline rendered on top
// of the canvas.
export type OverlayLayerKey =
  | "raw"
  | "tracks"
  | "labels"
  | "events"
  | "metrics"
  | "wireframe";

// ── Settings (Issue #112) ───────────────────────────────────────────────────

export type AutoExportAccess = "off" | "staff" | "all";

export interface SystemConfig {
  team_name: string;
  capture_camera: string;
  storage_bucket: string;
  auto_export_access: AutoExportAccess;
  // Kick off the processing pipeline automatically when a video is registered
  // (system default ON; the Film Room "Process Film" CTA remains for
  // manual/retry runs).
  auto_process_on_upload: boolean;
}

export interface ModelSensitivity {
  boundary_sensitivity: number;
  identity_confidence: number;
  motion_minimum: number;
  pose_review_gate: number;
}

export interface SystemSettingsResponse {
  system_config: SystemConfig;
  model_sensitivity: ModelSensitivity;
}

export interface SystemSettingsUpdate {
  system_config?: Partial<SystemConfig>;
  model_sensitivity?: Partial<ModelSensitivity>;
}

export interface UserPreferences {
  theme: "light" | "dark" | "system";
  default_session_kind: "all" | "practice" | "scrimmage" | "game";
  default_side_of_ball: "all" | "offense" | "defense" | "special";
}

// ── Reports (Issue #111) ────────────────────────────────────────────────────

export type ReportFormat = "pdf" | "csv" | "json";
export type ReportStatus = "queued" | "running" | "succeeded" | "failed" | "cancelled";

export interface ReportJob {
  id: string;
  report_type: string;
  format: ReportFormat;
  status: ReportStatus;
  parameters: { sections?: string[] } | null;
  output_uri: string | null;
  error_message: string | null;
  requested_by: string;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
}

export interface ReportCreateRequest {
  report_type?: string;
  format?: ReportFormat;
  sections?: string[];
}

export interface ReportDownloadResponse {
  download_url: string;
  expires_at: string;
}

// ── CFBD cached analytics (Issue #163) ──────────────────────────────────────
// All of these come from the Football-IQ backend, which reads cached
// CollegeFootballData.com rows. The frontend never calls CFBD directly and
// never sees the CFBD API key.

export type CfbdSyncStatus = "ok" | "partial" | "error" | "running" | "never";

export interface CfbdCacheMeta {
  source: string; // "CollegeFootballData.com"
  source_endpoint: string;
  sync_status: CfbdSyncStatus;
  last_synced_at: string | null;
  stale: boolean;
  stale_after_hours: number;
  row_count: number;
}

export interface CfbdTeam {
  cfbd_team_id: number | null;
  school: string;
  mascot: string | null;
  abbreviation: string | null;
  conference: string | null;
  division: string | null;
  color: string | null;
  alt_color: string | null;
}

export interface CfbdTeamResponse {
  team: CfbdTeam | null;
  cache: CfbdCacheMeta;
}

export interface CfbdGame {
  cfbd_game_id: number;
  season: number;
  week: number | null;
  season_type: string | null;
  start_date: string | null;
  home_team: string | null;
  away_team: string | null;
  home_points: number | null;
  away_points: number | null;
  venue: string | null;
  completed: boolean | null;
}

export interface CfbdScheduleResponse {
  season: number | null;
  games: CfbdGame[];
  cache: CfbdCacheMeta;
}

export interface CfbdMacBenchmarkRow {
  team: string;
  conference: string | null;
  games: number;
  avg_points_for: number | null;
  avg_points_against: number | null;
  point_differential: number | null;
}

export interface CfbdMacBenchmarkResponse {
  season: number | null;
  conference: string;
  teams: CfbdMacBenchmarkRow[];
  cache: CfbdCacheMeta;
}

// ── Zero-shot concept search (Issue #144) ────────────────────────────────────

export interface ConceptSearchMatch {
  concept_id: string;
  display_name: string;
  category: string;
  confidence: number;
}

export interface ConceptSearchResult {
  clip_id: string;
  source: "metadata" | "embedding" | string;
  confidence: number;
  score: number | null;
  is_experimental: boolean;
  matched_concept_ids: string[];
  label_data: Record<string, unknown> | null;
}

export interface ConceptSearchResponse {
  query: string;
  matched_concepts: ConceptSearchMatch[];
  approximate: boolean;
  experimental: boolean;
  reason: string | null;
  model_version_label: string | null;
  results: ConceptSearchResult[];
}
