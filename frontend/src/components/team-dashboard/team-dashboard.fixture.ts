// Test fixture for the Home dashboard (unit tests and local screenshots only).
import type { ApiTeamDashboard, ApiTeamWeek } from "@/lib/api";

function week(week_start: string, session_kind: string, reps: number, top: number, hsy: number): ApiTeamWeek {
  return {
    week_start,
    session_kind,
    reps,
    players: Math.round(reps / 6),
    yards_per_rep: 14 + hsy / 2,
    high_speed_yards_per_rep: hsy,
    sprints_per_rep: hsy / 10,
    top_speed_p90_yps: top,
    get_off_yards: session_kind === "game" ? 2.4 : null,
  };
}

export function sampleTeamDashboard(overrides: Partial<ApiTeamDashboard> = {}): ApiTeamDashboard {
  return {
    weekly: [
      week("2026-08-10", "practice", 220, 8.1, 3.2),
      week("2026-08-17", "practice", 260, 8.3, 3.6),
      week("2026-08-24", "practice", 240, 8.4, 3.9),
      week("2026-08-31", "practice", 210, 8.2, 3.5),
      week("2026-08-31", "game", 140, 8.9, 5.1),
      week("2026-09-07", "practice", 230, 8.5, 4.0),
      week("2026-09-07", "game", 150, 9.1, 5.6),
    ],
    position_groups: [
      {
        position_group: "WR",
        weekly: [
          { week_start: "2026-08-31", reps: 60, top_speed_p90_yps: 9.4, high_speed_yards_per_rep: 7.1, get_off_yards: null },
          { week_start: "2026-09-07", reps: 64, top_speed_p90_yps: 9.7, high_speed_yards_per_rep: 7.8, get_off_yards: null },
        ],
        change: { top_speed_p90_yps: 0.3, high_speed_yards_per_rep: 0.7, get_off_yards: null },
      },
      {
        position_group: "OL",
        weekly: [{ week_start: "2026-09-07", reps: 80, top_speed_p90_yps: 5.2, high_speed_yards_per_rep: 0.4, get_off_yards: null }],
        change: { top_speed_p90_yps: null, high_speed_yards_per_rep: null, get_off_yards: null },
      },
    ],
    games: [
      {
        video_id: "g-1",
        date: "2026-08-30",
        opponent: "Western Kentucky",
        linked_plays: 141,
        offense: { plays: 70, yards_per_play: 5.6, success_rate: 0.44, epa_per_play: 0.05, third_down_rate: 0.38, explosive_plays: 7, turnovers: 1 },
        defense: { plays: 71, yards_per_play: 5.1, success_rate: 0.41, epa_per_play: -0.04, third_down_rate: 0.33, explosive_plays: 5, turnovers: 2 },
      },
      {
        video_id: "g-2",
        date: "2026-09-05",
        opponent: "Michigan State",
        linked_plays: 150,
        offense: { plays: 74, yards_per_play: 6.2, success_rate: 0.47, epa_per_play: 0.11, third_down_rate: 0.42, explosive_plays: 9, turnovers: 0 },
        defense: { plays: 76, yards_per_play: 5.8, success_rate: 0.45, epa_per_play: 0.02, third_down_rate: 0.4, explosive_plays: 8, turnovers: 1 },
      },
    ],
    readiness: {
      visible: true,
      player_level: false,
      as_of: "2026-09-07",
      groups: [
        { position_group: "WR", player_count: 9, mean_acwr: 1.21, mean_injury_risk_score: 0.42, max_injury_risk_score: 0.66 },
        { position_group: "OL", player_count: 14, mean_acwr: 0.98, mean_injury_risk_score: 0.24, max_injury_risk_score: 0.39 },
      ],
      flagged: [],
    },
    activity: { sessions_this_week: 3, plays_this_week: 412, film_processing: 1, film_uploaded: 0 },
    ...overrides,
  };
}
