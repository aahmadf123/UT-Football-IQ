import { apiBase } from "./endpoints";
import type { CfbdCacheMeta } from "./types";

export interface CfbdConnectionStatus { configured: boolean; source: string }
export type CfbdSyncScope = "teams" | "schedule" | "benchmark" | "plays" | "drives";
export interface CfbdSyncResult {
  scope: CfbdSyncScope;
  season: number;
  status: "succeeded" | "partial" | "failed";
  rows_written: number;
  message: string;
}
export interface CfbdGamePlay {
  cfbd_play_id: number;
  offense: string | null;
  defense: string | null;
  period: number | null;
  down: number | null;
  distance: number | null;
  yard_line: number | null;
  yards_gained: number | null;
  play_type: string | null;
  play_text: string | null;
}
export interface CfbdGamePlays {
  game_id: number;
  plays: CfbdGamePlay[];
  cache: CfbdCacheMeta;
}

async function request<T>(path: string, token?: string, body?: unknown): Promise<T> {
  const response = await fetch(`${apiBase()}/api/cfbd${path}`, {
    method: body === undefined ? "GET" : "POST",
    headers: {
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(body === undefined ? {} : { "Content-Type": "application/json" }),
    },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  if (!response.ok) {
    const value: unknown = await response.json().catch(() => null);
    const detail = value && typeof value === "object" && "detail" in value ? value.detail : null;
    throw new Error(typeof detail === "string" ? detail : `College data request failed (${response.status}).`);
  }
  return response.json() as Promise<T>;
}

export function fetchCfbdConnection(token?: string): Promise<CfbdConnectionStatus> {
  return request("/status", token);
}

export function refreshCfbdScope(
  body: { season: number; scope: CfbdSyncScope; season_type: "regular" | "postseason"; week?: number },
  token?: string,
): Promise<CfbdSyncResult> {
  return request("/sync", token, body);
}

export function fetchCfbdGamePlays(gameId: number, token?: string): Promise<CfbdGamePlays> {
  return request(`/games/${gameId}/plays?limit=500`, token);
}
