// Athlete health/workload surface contracts (Issue #113), mirrored from the
// backend `app.health_workload` module so the UI can render the placeholder
// integration status before a real session/feed exists. The backend
// `GET /api/v1/health-workload/surface` endpoint is the authoritative source
// once an auth/session flow is wired; until then the page renders these
// placeholders statically.

export type IntegrationSource = "wellness" | "gps_wearables" | "strength_conditioning";

export type IntegrationStatus = "not_connected" | "connected";

export interface HealthWorkloadIntegration {
  source: IntegrationSource;
  displayName: string;
  description: string;
  status: IntegrationStatus;
  dataCategories: string[];
  providerExamples: string[];
}

// Shown verbatim. Reviewed so the surface never implies a medical device, a
// diagnosis, or an injury prediction. Mirrors backend
// `app.health_workload.SURFACE_DISCLAIMER` exactly (Issue #149 amended it
// with the experimental workload-risk carve-out).
export const HEALTH_WORKLOAD_DISCLAIMER =
  "Training-load and wellness context for sports-performance staff only. " +
  "This surface is not a medical device and does not diagnose injuries or " +
  "predict injury risk. Experimental workload-risk indicators are " +
  "sports-performance signals for staff review — not a diagnosis. " +
  "It supports staff judgement; it does not replace it.";

// Per-row caveat mirrored from backend `WORKLOAD_RISK_CAVEAT` — attached to
// every workload-risk value the UI renders.
export const WORKLOAD_RISK_CAVEAT =
  "Experimental sports-performance indicator — not a diagnosis and not a " +
  "medical prediction of injury.";

// ── Workload-risk surface types (Issue #149) ─────────────────────────────────
// Shapes returned by GET /api/v1/health-workload/injury-risk.

export interface InjuryRiskSeriesPoint {
  date: string;
  daily_load: number | null;
  sprint_count: number | null;
  max_speed_yps: number | null;
  asymmetry_index: number | null;
  acute_load_7d: number | null;
  chronic_load_28d: number | null;
  acwr: number | null;
  injury_risk_score: number | null;
  risk_reason_codes: string[];
  clip_count: number;
  attribution: string;
  confidence: number | null;
  model_variant: string | null;
}

export interface InjuryRiskPlayerRow {
  player_id: string;
  name: string;
  jersey_number: number | null;
  position_group: string | null;
  series: InjuryRiskSeriesPoint[];
  latest: InjuryRiskSeriesPoint | null;
  caveat: string;
}

export interface InjuryRiskAggregate {
  position_group: string;
  player_count: number;
  mean_acwr: number | null;
  mean_asymmetry_index: number | null;
  mean_injury_risk_score: number | null;
  max_injury_risk_score: number | null;
  caveat: string;
}

export interface InjuryRiskResponse {
  role: string;
  date: string;
  days: number;
  player_level: boolean;
  disclaimer: string;
  caveat: string;
  rows?: InjuryRiskPlayerRow[];
  aggregates?: InjuryRiskAggregate[];
}

// ── Daily athlete-state dashboard types (Issue #9) ───────────────────────────
// Shapes returned by GET /api/v1/health-workload/dashboard.

// Mirrors backend `app.health_workload.POLICY_STATEMENT` verbatim.
export const HEALTH_POLICY_STATEMENT =
  "CV outputs are workload/movement context that support staff judgment — " +
  "they are not a medical diagnosis, and no value on this surface is ground " +
  "truth. Staff decisions always take precedence.";

export interface SourceBlock {
  source: string;
  caveat: string;
  [key: string]: unknown;
}

export interface DailyAthleteState {
  player_id: string;
  name: string;
  jersey_number: number | null;
  position_group: string | null;
  cv_workload: (SourceBlock & {
    daily_load: number | null;
    sprint_count: number | null;
    asymmetry_index: number | null;
    acwr: number | null;
    injury_risk_score: number | null;
    confidence: number | null;
  }) | null;
  wellness: SourceBlock | null;
  gps: SourceBlock | null;
  strength_conditioning: SourceBlock | null;
  injury_history: Array<Record<string, unknown>> | null;
  caveats: string[];
}

export interface DashboardAggregate {
  position_group: string;
  player_count: number;
  mean_acwr: number | null;
  mean_asymmetry_index: number | null;
  mean_daily_load: number | null;
  caveat: string;
}

export interface TrendPoint {
  date: string;
  mean_acwr: number | null;
  mean_daily_load: number | null;
  player_count: number;
}

export interface FatigueFlag {
  alert_id: string;
  player_id: string | null;
  position_group: string;
  severity: string;
  metric_value: Record<string, unknown>;
  created_at: string;
  caveat: string;
}

export interface HealthDashboardResponse {
  date: string;
  days: number;
  role: string;
  player_level: boolean;
  policy_statement: string;
  caveat: string;
  academic_context: Array<Record<string, unknown>>;
  source_counts: Record<string, number>;
  players?: DailyAthleteState[];
  aggregates?: DashboardAggregate[];
  trends: Record<string, TrendPoint[]>;
  fatigue_flags?: FatigueFlag[];
  integrations: Array<{ source: string; display_name: string; status: string }>;
}

export const HEALTH_WORKLOAD_INTEGRATIONS: readonly HealthWorkloadIntegration[] = [
  {
    source: "wellness",
    displayName: "Wellness self-report",
    description:
      "Athlete-submitted daily wellness check-ins. Self-reported context only — never a clinical assessment.",
    status: "not_connected",
    dataCategories: ["Self-reported soreness", "Self-reported sleep", "Self-reported energy"],
    providerExamples: ["Team wellness questionnaire", "Athlete check-in app"],
  },
  {
    source: "gps_wearables",
    displayName: "GPS / wearables",
    description:
      "Practice and game movement load from GPS units and wearables — distance, speed bands, and accelerations as training-load context.",
    status: "not_connected",
    dataCategories: ["Total distance", "High-speed distance", "Accelerations", "Player load"],
    providerExamples: ["GPS tracking vest", "Wrist / chest wearable"],
  },
  {
    source: "strength_conditioning",
    displayName: "Strength & conditioning",
    description:
      "Weight-room and conditioning volume logged by S&C staff — session load and tonnage as planning context.",
    status: "not_connected",
    dataCategories: ["Session volume", "Tonnage", "Session RPE"],
    providerExamples: ["S&C session log", "Weight-room tracking sheet"],
  },
];
