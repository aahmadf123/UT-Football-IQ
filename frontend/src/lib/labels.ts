/**
 * Coach-facing labels for backend enums (single source of truth — previously
 * duplicated across library and clip-review).
 */

import type { OurPossession, PageKey, SessionKind } from "./types";

export const POSSESSION_LABEL: Record<OurPossession, string> = {
  offense: "Toledo Offense",
  defense: "Toledo Defense",
  special_teams: "Special Teams",
};

export const SESSION_KIND_LABEL: Record<SessionKind, string> = {
  practice: "Practice",
  scrimmage: "Scrimmage",
  game: "Game",
};

/**
 * Page titles/subtitles consumed by PageHeader and the app shell. Real product
 * copy — moved out of mock-data.ts so nothing coach-facing lives beside
 * fabricated sample data (repo rule #96).
 */
export const pageTitles: Record<PageKey, { title: string; subtitle: string }> = {
  dashboard: {
    title: "Practice & Game Intelligence Cockpit",
    subtitle: "Processed football intelligence coaching review",
  },
  "film-room": {
    title: "Film Room",
    subtitle: "Browse film, review & tag plays, build clips, and upload/process new film",
  },
  scouting: {
    title: "Scouting",
    subtitle: "Our tendencies, opponent prep, and college data context",
  },
  "video-and-plays": {
    title: "Video & Plays",
    subtitle: "Clip review, play correction, boundaries, and call signs",
  },
  players: {
    title: "Players",
    subtitle: "Roster, identity confidence, and development snapshots",
  },
  analytics: {
    title: "Model Insights",
    subtitle: "Model-derived metrics, formation recognition, tracking quality, and model outputs",
  },
  "self-scout": {
    title: "Self-Scout",
    subtitle: "What opponents can see in Toledo tendencies",
  },
  "opponent-scout": {
    title: "Opponent Scout",
    subtitle: "Imported opponent film, tells, and game-plan notes",
  },
  "player-development": {
    title: "Player Development",
    subtitle: "Position-specific passport, biomechanics, clips, and goals",
  },
  "health-workload": {
    title: "Health & Workload",
    subtitle:
      "Training-load and wellness context for sports-performance staff — not a medical or injury-risk tool",
  },
  reports: {
    title: "Reports",
    subtitle: "Coach-ready exports, weekly briefs, and automated sections",
  },
  "clips-highlights": {
    title: "Clips & Highlights",
    subtitle: "Cutups, teaching clips, and recruiting-ready highlights",
  },
  library: {
    title: "Library",
    subtitle: "Practice and game film grouped by date, session, opponent, and possession",
  },
  alerts: {
    title: "Coaching Alerts",
    subtitle: "Live stream of analytics and biomechanics flags from the pipeline",
  },
  "clip-review": {
    title: "Clip Review",
    subtitle: "Backend-backed clip metadata, processing status, and playback shell",
  },
  "college-data": {
    title: "College Data (CFBD)",
    subtitle: "Cached Toledo / MAC analytics from CollegeFootballData.com",
  },
  settings: {
    title: "Settings",
    subtitle: "Team configuration, roles, model sensitivity, and integrations",
  },
};
