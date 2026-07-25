import type { FootballData, SelfScoutResponse } from "./types";

const sampleSelfScout: SelfScoutResponse = {
  clip_count: 112,
  formation_tendencies: [
    trend("Trips Right", 28, 11, 17, ["c11", "c18"], false),
    trend("Doubles", 24, 16, 8, ["c22"], false),
    trend("Pistol", 18, 14, 4, ["c31"], false),
    trend("Empty", 9, 1, 8, ["c48"], true),
  ],
  motion_tendencies: {
    with_motion: split(34, 22, 12),
    without_motion: split(78, 36, 42),
  },
  field_zone_tendencies: [
    trend("Red Zone", 23, 16, 7, [], false),
    trend("Mid Field", 63, 30, 33, [], false),
    trend("Backed Up", 12, 8, 4, [], false),
  ],
  personnel_tendencies: [
    trend("11", 64, 26, 38, [], false),
    trend("12", 28, 19, 9, [], false),
    trend("21", 11, 8, 3, [], false),
  ],
  down_distance_tendencies: [
    { ...trend("1st / Long", 36, 20, 16, [], false), down: 1, distance_bucket: "long" },
    { ...trend("2nd / Medium", 21, 7, 14, [], false), down: 2, distance_bucket: "medium" },
    { ...trend("3rd / Long", 15, 2, 13, [], false), down: 3, distance_bucket: "long" },
  ],
  formation_concept_families: {
    "Trips Right": [
      { formation: "Trips Right", concept_family: "inside_zone", total_plays: 8, rate: 0.29, evidence_clip_ids: [], low_sample: false },
      { formation: "Trips Right", concept_family: "mesh", total_plays: 7, rate: 0.25, evidence_clip_ids: [], low_sample: false },
      { formation: "Trips Right", concept_family: "screen", total_plays: 5, rate: 0.18, evidence_clip_ids: [], low_sample: true },
    ],
  },
  pre_snap_tells: [
    {
      grouping_key: "pistol|without_motion",
      formation: "Pistol",
      motion_state: "without_motion",
      total_plays: 24,
      lean: "run",
      severity: "high",
      run_rate: 0.92,
      pass_rate: 0.08,
      evidence_clip_ids: ["c4", "c5"],
      low_sample: false,
      message: "Pistol without motion leans run at 92%.",
    },
    {
      grouping_key: "empty|without_motion",
      formation: "Empty",
      motion_state: "without_motion",
      total_plays: 12,
      lean: "pass",
      severity: "high",
      run_rate: 0.08,
      pass_rate: 0.92,
      evidence_clip_ids: ["c9"],
      low_sample: false,
      message: "Empty without motion is a pass tell.",
    },
  ],
  alerts: [
    { alert_type: "formation", message: "High run tendency from Pistol", severity: "high", grouping_key: "Pistol", run_rate: 0.92, pass_rate: 0.08 },
    { alert_type: "motion", message: "Motion raises run rate by 19 points", severity: "medium", grouping_key: "motion", run_rate: 0.65, pass_rate: 0.35 },
  ],
};

export const footballData: FootballData = {
  videos: [
    video("v1", "Practice 5-14 All-22.mp4", "ready"),
    video("v2", "Red Zone Team Period.mp4", "processing"),
    video("v3", "Opponent Cutup Import.mp4", "uploaded"),
  ],
  jobs: [
    job("Ingestion", "succeeded", 10, "same_session"),
    job("Calibration", "succeeded", 10, "same_session"),
    job("Detection", "succeeded", 10, "same_session"),
    job("Tracking", "running", 10, "same_session"),
    job("Pose Estimation", "queued", 10, "same_session"),
    job("render", "queued", 10, "same_session"),
    job("Detection (Full)", "queued", 0, "nightly"),
    job("render_hls", "queued", 0, "nightly"),
  ],
  selfScout: sampleSelfScout,
  players: [
    player("11", "M. Carter", "WR", "Skill", 18.7, 112.3, 2.4, 0.82, [21, 34, 48, 57, 63, 71]),
    player("28", "J. Hill", "RB", "Skill", 19.6, 98.7, 1.9, 0.79, [18, 26, 40, 44, 59, 66]),
    player("54", "D. Evans", "C", "OL", 12.8, 89.5, 0.7, 0.91, [40, 42, 41, 45, 48, 49]),
    player("9", "A. Moore", "QB", "QB", 16.2, 76.4, 3.2, 0.86, [30, 36, 39, 48, 53, 58]),
  ],
  plays: [
    { number: 42, formation: "Trips Right", personnel: "11", concept: "Inside Zone", result: "Gain", yards: 8, confidence: 0.92 },
    { number: 43, formation: "Doubles", personnel: "11", concept: "Mesh", result: "Complete", yards: 14, confidence: 0.88 },
    { number: 44, formation: "Pistol", personnel: "12", concept: "Duo", result: "Gain", yards: 5, confidence: 0.81 },
    { number: 45, formation: "Empty", personnel: "10", concept: "Stick", result: "Flagged", yards: 0, confidence: 0.64 },
  ],
  clips: [
    { id: "clip1", title: "#11 vs Man Coverage", subtitle: "Great separation", duration: "00:12", tag: "WR" },
    { id: "clip2", title: "Inside Zone", subtitle: "Explosive run", duration: "00:15", tag: "Run" },
    { id: "clip3", title: "Red Zone PA Boot", subtitle: "QB read", duration: "00:10", tag: "QB" },
    { id: "clip4", title: "Boundary Error", subtitle: "Needs trim", duration: "00:09", tag: "QA" },
    { id: "clip5", title: "Pad Level", subtitle: "#75 RT", duration: "00:08", tag: "OL" },
    { id: "clip6", title: "Motion Tell", subtitle: "Pre-snap", duration: "00:11", tag: "Scout" },
  ],
  health: [
    { player: "#28 RB", load: "High", status: "High" },
    { player: "#11 WR", load: "High", status: "High" },
    { player: "#54 C", load: "Medium", status: "Med" },
  ],
  alerts: [
    { title: "Strong success rate on Inside Zone", detail: "78% positive reps", severity: "good" },
    { title: "#75 RT pad level inconsistent", detail: "Position coach review", severity: "danger" },
    { title: "#3 CB eyes in backfield on PA", detail: "Technique alert", severity: "warning" },
  ],
};

function trend(grouping_key: string, total: number, run: number, pass: number, evidence_clip_ids: string[], low_sample: boolean) {
  return {
    grouping_key,
    total_plays: total,
    run_count: run,
    pass_count: pass,
    run_rate: run / total,
    pass_rate: pass / total,
    evidence_clip_ids,
    low_sample,
  };
}

function split(total: number, run: number, pass: number) {
  return { total, run_count: run, pass_count: pass, run_rate: run / total, pass_rate: pass / total };
}

function video(id: string, filename: string, status: string) {
  return { id, filename, status, duration_seconds: 7080, fps: 60, width: 3840, height: 2160, created_at: "2026-05-14T14:00:00Z" };
}

function job(job_type: string, status: string, priority: number, pipeline_mode?: string) {
  return {
    id: job_type.toLowerCase().replaceAll(" ", "-"),
    job_type,
    status,
    priority,
    pipeline_mode: pipeline_mode ?? (priority >= 10 ? "same_session" : "nightly"),
    is_same_session: priority >= 10,
    created_at: "2026-05-14T14:00:00Z",
  };
}

function player(jersey: string, name: string, position: string, group: string, maxSpeed: number, distance: number, separation: number, confidence: number, trendData: number[]) {
  return { id: jersey, jersey, name, position, group, maxSpeed, distance, separation, confidence, trend: trendData };
}
