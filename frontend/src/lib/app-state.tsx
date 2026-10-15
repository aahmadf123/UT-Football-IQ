"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { footballData } from "./mock-data";
import { emptyFootballData } from "./empty-data";
import { useMocks } from "./mock-flag";
import { useOptionalAuth } from "./auth";
import { resolveCurrentRole, type UserRole } from "./roles";
import { toast } from "sonner";
import {
  fetchInboxStatus,
  fetchJobs,
  fetchPlayerMetricsSummaries,
  fetchPlayers,
  fetchVideos,
  registerVideo,
  requestUploadUrl,
  uploadToObjectStore,
} from "./api";
import type { ApiPlayerMetricsSummary, VideoInboxItem } from "./api";
import type {
  ApiJob,
  ApiPlayer,
  ApiVideo,
  FootballData,
  PlayerSummary,
  SessionKind,
  SourceType,
  OurPossession,
} from "./types";

export type SessionType = "all" | "practice" | "game" | "scrimmage";
export type ApiStatus = "idle" | "loading" | "live" | "offline" | "error" | "mock";

const SESSION_LABELS: Record<SessionType, string> = {
  all: "Practice & Games",
  practice: "Practice Only",
  game: "Games Only",
  scrimmage: "Scrimmages",
};

// Fallback group derivation when a player row hasn't been tagged yet. Maps
// positions to the coaching-group labels used by the roster filters (Skill,
// OL, DL, LB, DB, ST, QB).
const GROUP_BY_POSITION: Record<string, string> = {
  QB: "QB",
  RB: "Skill",
  WR: "Skill",
  TE: "Skill",
  OL: "OL",
  C: "OL",
  G: "OL",
  T: "OL",
  DL: "DL",
  DE: "DL",
  DT: "DL",
  LB: "LB",
  MLB: "LB",
  OLB: "LB",
  CB: "DB",
  S: "DB",
  FS: "DB",
  SS: "DB",
  DB: "DB",
  K: "ST",
  P: "ST",
  LS: "ST",
  RET: "ST",
};

function looksNetworkFailure(error: unknown): boolean {
  if (!(error instanceof Error)) return false;
  const msg = error.message.toLowerCase();
  // Browser fetch/network failures (including CORS-blocked requests) surface
  // as "Failed to fetch"/"NetworkError" rather than an HTTP status.
  return (
    msg.includes("failed to fetch") ||
    msg.includes("networkerror") ||
    msg.includes("load failed") ||
    msg.includes("fetch failed")
  );
}

function deriveGroup(position: string | null, positionGroup: string | null): string {
  if (positionGroup) return positionGroup;
  if (position && GROUP_BY_POSITION[position]) return GROUP_BY_POSITION[position];
  return "Unassigned";
}

/**
 * Convert a backend ``ApiPlayer`` into the UI-side ``PlayerSummary``.
 *
 * Performance/confidence fields start ``undefined`` here; they are filled by
 * :func:`mergePlayerMetrics` from ``/api/v1/players/metrics/summary``. A
 * player with no tracked film keeps them undefined and the UI shows "—"
 * instead of fabricating numbers.
 */
export function apiPlayerToSummary(p: ApiPlayer): PlayerSummary {
  const firstName = p.first_name.trim();
  const name = firstName
    ? `${firstName.charAt(0)}. ${p.last_name}`.trim()
    : `${firstName} ${p.last_name}`.trim();
  return {
    id: p.id,
    jersey: p.jersey_number != null ? String(p.jersey_number) : "—",
    name,
    position: p.position ?? "Unassigned",
    group: deriveGroup(p.position, p.position_group),
  };
}

// The pipeline measures speed in yd/s (field coordinates are yards); coaches
// read MPH. 1 yd/s = 0.9144 m/s = 2.0455 mph.
export const YDS_PER_SEC_TO_MPH = 2.0455;

/** Merge batched tracking aggregates onto roster summaries by player id. */
export function mergePlayerMetrics(
  players: PlayerSummary[],
  rows: ApiPlayerMetricsSummary[],
): PlayerSummary[] {
  if (!Array.isArray(rows) || rows.length === 0) return players;
  const byId = new Map(rows.map((r) => [r.player_id, r]));
  return players.map((p) => {
    const m = byId.get(p.id);
    if (!m) return p;
    return {
      ...p,
      maxSpeed:
        m.max_speed_yps != null
          ? Math.round(m.max_speed_yps * YDS_PER_SEC_TO_MPH * 10) / 10
          : undefined,
      distance: m.distance_yards != null ? Math.round(m.distance_yards) : undefined,
      confidence: m.tracking_confidence ?? undefined,
      identityBucket: m.identity_bucket,
      trackedClips: m.tracked_clip_count,
      lastTrackedAt: m.last_tracked_at ?? undefined,
    };
  });
}

const STORAGE_KEY = "football_iq_app_state_v1";

export type UploadPhase = "idle" | "requesting-url" | "uploading" | "registering" | "done" | "error";

export interface UploadedClip {
  id: string;
  filename: string;
  objectUrl?: string;
  sizeBytes: number;
  uploadedAt: string;
  storageUri?: string;
  videoId?: string;
  phase: UploadPhase;
  progress: number;
  error?: string;
}

export interface UploadMetadata {
  recorded_at?: string | null;
  session_kind?: SessionKind | null;
  source_type?: SourceType | null;
  opponent_team?: string | null;
  our_possession?: OurPossession | null;
  tags?: string[];
  practice_session_id?: string | null;
  library_role?: "toledo" | "opponent";
  camera_label?: string | null;
}

interface PersistedState {
  sessionType: SessionType;
  selectedDate: string;
}

interface AppStateValue {
  // Filters
  sessionType: SessionType;
  setSessionType: (v: SessionType) => void;
  selectedDate: string;
  setSelectedDate: (v: string) => void;
  availableDates: string[];

  // Data
  data: FootballData;

  // Connectivity
  apiStatus: ApiStatus;
  apiError?: string;
  playersStatus: ApiStatus;
  mockMode: boolean;

  // Selection
  selectedPlayerId: string;
  setSelectedPlayerId: (id: string) => void;
  selectedPlayer: PlayerSummary | undefined;
  getPlayerById: (id: string) => PlayerSummary | undefined;

  // Upload
  uploads: UploadedClip[];
  addUploads: (files: FileList | File[], metadata?: UploadMetadata) => Promise<UploadedClip[]>;
  retryUpload: (id: string) => void;
  removeUpload: (id: string) => void;

  // Inbox
  inboxItems: VideoInboxItem[];
  refreshInbox: () => void;
  refreshData: () => Promise<void>;

  // Auth
  authToken?: string;
  // Effective role used for client-side surface gating (Issue #113). Derived
  // from the JWT role claim, a NEXT_PUBLIC_DEMO_ROLE override, or a safe
  // "coach" default. Never a security boundary — the backend re-checks.
  currentRole: UserRole;
}

const AppStateContext = createContext<AppStateValue | null>(null);

function loadPersisted(): PersistedState | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    return JSON.parse(raw) as PersistedState;
  } catch {
    return null;
  }
}

function savePersisted(state: PersistedState) {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
  } catch {
    /* ignore */
  }
}

function buildDates(uploads: UploadedClip[], videos: ApiVideo[]): string[] {
  const set = new Set<string>();
  for (const v of videos) {
    const recorded = v.recorded_at ?? v.created_at;
    if (recorded) set.add(recorded.slice(0, 10));
  }
  for (const u of uploads) {
    set.add(u.uploadedAt.slice(0, 10));
  }
  return Array.from(set).sort().reverse();
}

export function AppStateProvider({
  children,
  authToken,
  getValidToken,
}: {
  children: React.ReactNode;
  authToken?: string;
  getValidToken?: () => Promise<string | undefined>;
}) {
  const mockMode = useMocks();
  const auth = useOptionalAuth();
  const canFetch = !auth || (auth.ready && (!auth.authRequired || Boolean(authToken)));
  const initialData = mockMode ? footballData : emptyFootballData;

  const [sessionType, setSessionType] = useState<SessionType>("all");
  const [selectedDate, setSelectedDate] = useState<string>("");
  const [uploads, setUploads] = useState<UploadedClip[]>([]);

  const [data, setData] = useState<FootballData>(initialData);
  const [apiStatus, setApiStatus] = useState<ApiStatus>(mockMode ? "mock" : "idle");
  const [apiError, setApiError] = useState<string>();
  const [playersStatus, setPlayersStatus] = useState<ApiStatus>(mockMode ? "mock" : "idle");
  const [selectedPlayerId, setSelectedPlayerId] = useState<string>(
    initialData.players[0]?.id ?? "",
  );

  // Inbox status from backend
  const [inboxItems, setInboxItems] = useState<VideoInboxItem[]>([]);

  // Keep a ref to the latest metadata for retries
  const retryMetaRef = useRef<Map<string, { file: File; metadata?: UploadMetadata }>>(new Map());
  const uploadAbortRef = useRef<Map<string, AbortController>>(new Map());

  // Auth token ref — updated from props, read by async upload/inbox calls
  const tokenRef = useRef(authToken);
  useEffect(() => { tokenRef.current = authToken; }, [authToken]);

  const resolveToken = useCallback(async (): Promise<string | undefined> => {
    if (getValidToken) {
      const refreshed = await getValidToken();
      if (refreshed) {
        tokenRef.current = refreshed;
        return refreshed;
      }
    }
    return tokenRef.current;
  }, [getValidToken]);

  // Only preferences persist in the browser. Film comes exclusively from durable storage.
  useEffect(() => {
    const saved = loadPersisted();
    if (saved) {
      setSessionType(saved.sessionType ?? "all");
      setSelectedDate(saved.selectedDate ?? "");
    }
  }, []);
  useEffect(() => {
    savePersisted({ sessionType, selectedDate });
  }, [sessionType, selectedDate]);

  const fetchRunRef = useRef(0);
  const refreshData = useCallback(async () => {
    if (mockMode || !canFetch) return;
    const run = ++fetchRunRef.current;
    const filters: Record<string, string> = {};
    if (selectedDate) {
      filters.recorded_after = `${selectedDate}T00:00:00Z`;
      filters.recorded_before = `${selectedDate}T23:59:59.999999Z`;
    }
    if (sessionType !== "all") filters.session_kind = sessionType;
    const token = tokenRef.current;
    const [videos, jobs, inbox, metrics, roster] = await Promise.allSettled([
      fetchVideos(filters, token), fetchJobs({}, token), fetchInboxStatus(token, true), fetchPlayerMetricsSummaries(token), fetchPlayers({ is_active: true, limit: 500 }, token),
    ]);
    if (run !== fetchRunRef.current) return;
    setData((current) => ({
      ...current,
      videos: pickArrLive<ApiVideo>(videos, current.videos),
      jobs: pickArrLive<ApiJob>(jobs, current.jobs),
      players: mergePlayerMetrics(roster.status === "fulfilled" && Array.isArray(roster.value) ? roster.value.map(apiPlayerToSummary) : current.players.map((player) => ({ ...player, maxSpeed: undefined, distance: undefined, confidence: undefined, identityBucket: undefined, trackedClips: undefined, lastTrackedAt: undefined })), metrics.status === "fulfilled" ? metrics.value : []),
    }));
    if (inbox.status === "fulfilled" && Array.isArray(inbox.value)) setInboxItems(inbox.value);
    const failures = [videos, jobs, inbox].filter((result): result is PromiseRejectedResult => result.status === "rejected");
    setApiError(failures.length ? failures.map((result) => result.reason instanceof Error ? result.reason.message : String(result.reason)).join("; ") : undefined);
    setApiStatus(failures.length === 0 ? "live" : failures.every((result) => looksNetworkFailure(result.reason)) ? "offline" : "error");
  }, [mockMode, selectedDate, sessionType, canFetch]);

  useEffect(() => {
    if (mockMode || !canFetch) return;
    setApiStatus("loading");
    void refreshData();
    const interval = window.setInterval(() => {
      if (document.visibilityState !== "hidden") void refreshData();
    }, 5000);
    const onFocus = () => void refreshData();
    window.addEventListener("focus", onFocus);
    return () => {
      ++fetchRunRef.current;
      window.clearInterval(interval);
      window.removeEventListener("focus", onFocus);
    };
  }, [mockMode, refreshData, authToken, canFetch]);

  // Fetch the active roster from /api/v1/players on mount and whenever
  // mockMode changes. Roster data is independent of the date/session filters
  // because the player table is roster-scoped, not film-scoped.
  useEffect(() => {
    if (!canFetch) return;
    if (mockMode) {
      setPlayersStatus("mock");
      return;
    }
    let cancelled = false;
    setPlayersStatus("loading");
    (async () => {
      try {
        // Metrics ride alongside the roster fetch; a metrics failure must
        // never take down the roster itself, so it degrades to "no numbers".
        const [apiPlayers, metricRows] = await Promise.all([
          fetchPlayers({ is_active: true, limit: 500 }, tokenRef.current),
          fetchPlayerMetricsSummaries(tokenRef.current).catch(
            () => [] as ApiPlayerMetricsSummary[],
          ),
        ]);
        if (cancelled) return;
        const summaries = mergePlayerMetrics(apiPlayers.map(apiPlayerToSummary), metricRows);
        setData((cur) => ({ ...cur, players: summaries }));
        setPlayersStatus("live");
      } catch (err) {
        // Keep "offline" only for genuine network/CORS failures.
        if (!cancelled) setPlayersStatus(looksNetworkFailure(err) ? "offline" : "error");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [mockMode, authToken, canFetch]);

  const refreshInbox = useCallback(() => { void refreshData(); }, [refreshData]);

  function updateUpload(id: string, patch: Partial<UploadedClip>) {
    setUploads((cur) => cur.map((u) => (u.id === id ? { ...u, ...patch } : u)));
  }

  async function executeUpload(clip: UploadedClip, file: File, metadata?: UploadMetadata) {
    if (mockMode) {
      updateUpload(clip.id, { phase: "error", error: "Uploads require a real local library. Demo mode does not save film." });
      return;
    }
    const controller = new AbortController();
    uploadAbortRef.current.set(clip.id, controller);
    try {
      // Step 1: Request a presigned upload URL from the backend
      updateUpload(clip.id, { phase: "requesting-url", progress: 0 });
      const token = await resolveToken();
      const { uploadUrl } = await requestUploadUrl(file.name, token);
      if (controller.signal.aborted) return;

      // Step 2: PUT the file to the presigned upload URL
      updateUpload(clip.id, { phase: "uploading", progress: 0 });
      let uploadResult;
      try {
        uploadResult = await uploadToObjectStore(uploadUrl, file, token, (loaded, total) => {
          const pct = Math.round((loaded / total) * 100);
          updateUpload(clip.id, { progress: pct });
        }, controller.signal);
      } catch (err) {
        const msg = err instanceof Error ? err.message.toLowerCase() : String(err).toLowerCase();
        const tokenExpired = msg.includes("401") && (msg.includes("expired token") || msg.includes("invalid token"));
        if (!tokenExpired) throw err;

        const retriedToken = await resolveToken();
        if (!retriedToken || retriedToken === token) throw err;

        uploadResult = await uploadToObjectStore(uploadUrl, file, retriedToken, (loaded, total) => {
          const pct = Math.round((loaded / total) * 100);
          updateUpload(clip.id, { progress: pct });
        }, controller.signal);
      }

      if (controller.signal.aborted) return;
      // Step 3: Register video with backend
      updateUpload(clip.id, { phase: "registering", progress: 100 });
      const registerToken = tokenRef.current;
      const video = await registerVideo({
        filename: file.name,
        storage_uri: uploadResult.storageUri,
        recorded_at: metadata?.recorded_at,
        session_kind: metadata?.session_kind,
        source_type: metadata?.source_type,
        opponent_team: metadata?.opponent_team,
        our_possession: metadata?.our_possession,
        tags: metadata?.tags,
        practice_session_id: metadata?.practice_session_id,
        library_role: metadata?.library_role ?? "toledo",
        camera_label: metadata?.camera_label,
      }, registerToken);

      updateUpload(clip.id, {
        phase: "done",
        progress: 100,
        storageUri: uploadResult.storageUri,
        videoId: video.id,
      });
      setData((current) => ({ ...current, videos: [video, ...current.videos.filter((row) => row.id !== video.id)] }));
      refreshInbox();
      toast.success(`${file.name} uploaded`, {
        description: "Saved in your library. Follow processing status in Film Room.",
      });
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      updateUpload(clip.id, { phase: "error", error: message });
      if (!controller.signal.aborted) toast.error(`Upload failed: ${file.name}`, { description: message });
    } finally {
      uploadAbortRef.current.delete(clip.id);
    }
  }

  const addUploads = useCallback(async (files: FileList | File[], metadata?: UploadMetadata) => {
    const arr = Array.from(files);
    const created: UploadedClip[] = arr.map((f, i) => ({
      id: `up-${Date.now()}-${i}`,
      filename: f.name,
      sizeBytes: f.size,
      uploadedAt: new Date().toISOString(),
      phase: "idle" as UploadPhase,
      progress: 0,
    }));

    setUploads((cur) => [...cur, ...created]);

    // Store file references for retries
    for (let i = 0; i < arr.length; i++) {
      retryMetaRef.current.set(created[i].id, { file: arr[i], metadata });
    }

    // Bound transfers to protect laptop memory and disk throughput.
    let next = 0;
    const drain = async () => {
      while (next < created.length) {
        const index = next++;
        if (retryMetaRef.current.has(created[index].id)) await executeUpload(created[index], arr[index], metadata);
      }
    };
    void drain();
    void drain();

    return created;
  }, [mockMode, refreshInbox, resolveToken]);

  const retryUpload = useCallback((id: string) => {
    const meta = retryMetaRef.current.get(id);
    if (!meta) return;
    const clip = uploads.find((u) => u.id === id);
    if (!clip) return;
    updateUpload(id, { phase: "idle", progress: 0, error: undefined });
    executeUpload(clip, meta.file, meta.metadata);
  }, [uploads, mockMode, refreshInbox, resolveToken]);

  const removeUpload = useCallback((id: string) => {
    uploadAbortRef.current.get(id)?.abort();
    uploadAbortRef.current.delete(id);
    setUploads((cur) => {
      const target = cur.find((u) => u.id === id);
      if (target?.objectUrl) {
        try { URL.revokeObjectURL(target.objectUrl); } catch { /* ignore */ }
      }
      return cur.filter((u) => u.id !== id);
    });
    retryMetaRef.current.delete(id);
  }, []);

  const getPlayerById = useCallback(
    (id: string) => data.players.find((p) => p.id === id || p.jersey === id),
    [data.players],
  );

  const selectedPlayer = useMemo(() => {
    return getPlayerById(selectedPlayerId) ?? data.players[0];
  }, [getPlayerById, selectedPlayerId, data.players]);

  const availableDates = useMemo(() => ["", ...buildDates(uploads, data.videos)], [uploads, data.videos]);

  const currentRole = useMemo(() => resolveCurrentRole(authToken), [authToken]);

  const value: AppStateValue = {
    sessionType,
    setSessionType,
    selectedDate,
    setSelectedDate,
    availableDates,
    data,
    apiStatus,
    apiError,
    playersStatus,
    mockMode,
    selectedPlayerId,
    setSelectedPlayerId,
    selectedPlayer,
    getPlayerById,
    uploads,
    addUploads,
    retryUpload,
    removeUpload,
    inboxItems,
    refreshInbox,
    refreshData,
    authToken,
    currentRole,
  };

  return <AppStateContext.Provider value={value}>{children}</AppStateContext.Provider>;
}

export function useAppState(): AppStateValue {
  const ctx = useContext(AppStateContext);
  if (!ctx) {
    throw new Error("useAppState must be used inside <AppStateProvider>");
  }
  return ctx;
}

export { SESSION_LABELS };

// Returns the API value as-is on success (including empty arrays — empty is
// valid live data, not a signal to substitute mock). Falls back only on a
// rejection or a non-array payload.
function pickArrLive<T>(r: PromiseSettledResult<unknown>, fallback: T[]): T[] {
  if (r.status === "fulfilled" && Array.isArray(r.value)) {
    return r.value as T[];
  }
  return fallback;
}
