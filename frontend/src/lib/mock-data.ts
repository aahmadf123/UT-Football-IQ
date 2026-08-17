import type { FootballData } from "./types";

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
  players: [
    player("11", "M. Carter", "WR", "Skill", 18.7, 112.3, 0.82, [21, 34, 48, 57, 63, 71]),
    player("28", "J. Hill", "RB", "Skill", 19.6, 98.7, 0.79, [18, 26, 40, 44, 59, 66]),
    player("54", "D. Evans", "C", "OL", 12.8, 89.5, 0.91, [40, 42, 41, 45, 48, 49]),
    player("9", "A. Moore", "QB", "QB", 16.2, 76.4, 0.86, [30, 36, 39, 48, 53, 58]),
  ],
};

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

function player(jersey: string, name: string, position: string, group: string, maxSpeed: number, distance: number, confidence: number, trendData: number[]) {
  return {
    id: jersey,
    jersey,
    name,
    position,
    group,
    maxSpeed,
    distance,
    confidence,
    identityBucket: "probable" as const,
    trackedClips: trendData.length,
    trend: trendData,
  };
}
