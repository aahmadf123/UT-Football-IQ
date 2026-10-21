import { apiBase } from "./endpoints";
import { apiErrorFromResponse, offlineError } from "./api-error";
import type { ApiClip } from "./types";

export interface ClipEditOperation {
  operation_id: string; operation_type: string; created_at?: string; state?: string;
  revisions: Record<string, number>; undo_available: boolean;
  undo_blocked_reason?: string;
  before?: Record<string, { start_seconds: number; end_seconds: number; revision: number }>;
  after?: Record<string, { start_seconds: number; end_seconds: number; revision: number }>;
}
export interface ClipEditResult extends ClipEditOperation { clips: ApiClip[]; superseded_clip_ids: string[] }
async function request<T>(path: string, token?: string, body?: unknown): Promise<T> {
  let response: Response;
  try { response = await fetch(`${apiBase()}/api/v1${path}`, { method: body == null ? "GET" : "POST", headers: { ...(token ? { Authorization: `Bearer ${token}` } : {}), ...(body != null ? { "Content-Type": "application/json" } : {}) }, ...(body != null ? { body: JSON.stringify(body) } : {}) }); }
  catch (cause) { throw offlineError("edit play boundaries", cause); }
  if (!response.ok) throw await apiErrorFromResponse(response, "edit play boundaries");
  return response.json() as Promise<T>;
}
export const splitClip = (id: string, at: number, revision: number, token?: string) => request<ClipEditResult>(`/clips/${encodeURIComponent(id)}/split`, token, { at_seconds: at, expected_revision: revision });
export const mergeClips = (ids: string[], revisions: Record<string, number>, token?: string) => request<ClipEditResult>("/clips/merge", token, { clip_ids: ids, expected_revisions: revisions });
export const undoClipEdit = (id: string, revisions: Record<string, number>, token?: string) => request<ClipEditResult>(`/clip-edits/${encodeURIComponent(id)}/undo`, token, { expected_revisions: revisions });
export const fetchClipEdits = (id: string, token?: string) => request<ClipEditOperation[]>(`/clips/${encodeURIComponent(id)}/edits`, token);
