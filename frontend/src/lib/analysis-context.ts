/** URL > saved view > personal defaults. Only known, typed filters travel. */
export interface AnalysisContext {
  season: string;
  since: string;
  until: string;
  session_kind: string;
  session_ids: string;
  library_role: string;
  side_of_ball: string;
  position: string;
  player_id: string;
  personnel: string;
  formation: string;
  situation: string;
  drill_type: string;
  contact_level: string;
  opponent: string;
  metric: string;
  group_by: "metric" | "player" | "position" | "session_kind";
  layout: "coach" | "analyst";
}
export const ANALYSIS_DEFAULTS: AnalysisContext = {
  season: "", since: "", until: "", session_kind: "", session_ids: "", library_role: "",
  side_of_ball: "", position: "", player_id: "", personnel: "", formation: "", situation: "",
  drill_type: "", contact_level: "", opponent: "",
  metric: "", group_by: "metric", layout: "coach",
};
export function resolveAnalysisContext(params: URLSearchParams, saved: Partial<AnalysisContext> = {}, personal: Partial<AnalysisContext> = {}): AnalysisContext {
  const result = { ...ANALYSIS_DEFAULTS };
  for (const key of Object.keys(ANALYSIS_DEFAULTS) as (keyof AnalysisContext)[]) {
    if (typeof personal[key] === "string") Object.assign(result, { [key]: personal[key] });
    if (typeof saved[key] === "string") Object.assign(result, { [key]: saved[key] });
    if (params.has(key)) Object.assign(result, { [key]: params.get(key) ?? "" });
  }
  if (!["coach", "analyst"].includes(result.layout)) result.layout = "coach";
  if (!["metric", "player", "position", "session_kind"].includes(result.group_by)) result.group_by = "metric";
  return result;
}
export function updateContextParams(params: URLSearchParams, patch: Partial<AnalysisContext>): URLSearchParams {
  const next = new URLSearchParams(params);
  for (const [key, value] of Object.entries(patch)) next.set(key, value ?? "");
  return next;
}
/** Keep filters, return location and selection when moving between plays. */
export function filmReviewHref(params: URLSearchParams, clipId: string, overrides: Record<string, string> = {}): string {
  const next = new URLSearchParams(params);
  next.set("clipId", clipId);
  next.delete("videoId");
  next.delete("t"); // New play starts at zero; angle switching supplies aligned time.
  for (const [key, value] of Object.entries(overrides)) next.set(key, value);
  return `/clip-review/?${next}`;
}

/** Open the active recording while retaining the cohort and return location. */
export function recordingReviewHref(params: URLSearchParams, videoId: string): string {
  const next = new URLSearchParams(params);
  for (const key of ["clipId", "t", "playlist", "trackletId"]) next.delete(key);
  next.set("videoId", videoId);
  return `/clip-review/?${next}`;
}

/** Carry the same analytical population into a profile without film-local state. */
export function playerProfileContextHref(params: URLSearchParams, playerId: string, from?: string): string {
  const next = new URLSearchParams();
  for (const key of Object.keys(ANALYSIS_DEFAULTS)) if (params.has(key)) next.set(key, params.get(key) ?? "");
  next.set("id", playerId);
  next.set("player_id", playerId);
  if (from ?? params.get("from")) next.set("from", safeReturnHref(from ?? params.get("from") ?? "", "/players/"));
  return `/players/detail/?${next}`;
}

export function safeReturnHref(value: string, fallback: string): string {
  if (!value.startsWith("/") || value.includes("\\") || /[\u0000-\u001f]/.test(value)) return fallback;
  try { return new URL(value, "https://film.local").origin === "https://film.local" ? value : fallback; }
  catch { return fallback; }
}
