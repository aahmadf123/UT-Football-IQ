"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { footballData } from "./mock-data";
import { emptyFootballData } from "./empty-data";
import { useMocks } from "./mock-flag";
import { resolveCurrentRole, type UserRole } from "./roles";
import {
  fetchInboxStatus,
  fetchJobs,
  fetchPlayers,
  fetchSelfScoutTendencies,
  fetchVideos,
  registerVideo,
  requestUploadUrl,
  uploadToR2,
} from "./api";
import type { VideoInboxItem } from "./api";
import type {
  ApiJob,
  ApiPlayer,
  ApiVideo,
  ClipSummary,
  FootballData,
  PlayerSummary,
  PlaySummary,
  SelfScoutResponse,
  SessionKind,
  SourceType,
  OurPossession,
} from "./types";

export type SessionType = "all" | "practice" | "game" | "scrimmage";
export type ApiStatus = "idle" | "loading" | "live" | "offline" | "mock";

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
 * Performance metrics (`maxSpeed`, `distance`, `separation`, `confidence`,
 * `trend`) are intentionally left ``undefined`` — the P1 players surface only
 * carries identity, and the UI shows "—" instead of fabricating numbers.
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
}

interface PersistedState {
  sessionType: SessionType;
  selectedDate: string;
  uploadedNames: string[];
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

function todayISO() {
  const d = new Date();
  return d.toISOString().slice(0, 10);
}

function buildDates(uploads: UploadedClip[], videos: ApiVideo[]): string[] {
  const set = new Set<string>();
  set.add(todayISO());
  for (const v of videos) {
    if (v.created_at) set.add(v.created_at.slice(0, 10));
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
  const initialData = mockMode ? footballData : emptyFootballData;

  const persisted = typeof window !== "undefined" ? loadPersisted() : null;

  const [sessionType, setSessionType] = useState<SessionType>(persisted?.sessionType ?? "all");
  const [selectedDate, setSelectedDate] = useState<string>(persisted?.selectedDate ?? "");
  const [uploads, setUploads] = useState<UploadedClip[]>([]);

  const [data, setData] = useState<FootballData>(initialData);
  const [apiStatus, setApiStatus] = useState<ApiStatus>(mockMode ? "mock" : "idle");
  const [playersStatus, setPlayersStatus] = useState<ApiStatus>(mockMode ? "mock" : "idle");
  const [selectedPlayerId, setSelectedPlayerId] = useState<string>(
    initialData.players[0]?.id ?? "",
  );

  // Inbox status from backend
  const [inboxItems, setInboxItems] = useState<VideoInboxItem[]>([]);

  // Keep a ref to the latest metadata for retries
  const retryMetaRef = useRef<Map<string, { file: File; metadata?: UploadMetadata }>>(new Map());

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

  // Hydrate uploaded clip names from storage on mount
  useEffect(() => {
    const p = loadPersisted();
    if (p?.uploadedNames?.length) {
      const rehydrated: UploadedClip[] = p.uploadedNames.map((name, i) => ({
        id: `persist-${i}-${name}`,
        filename: name,
        sizeBytes: 0,
        uploadedAt: new Date().toISOString(),
        phase: "done" as UploadPhase,
        progress: 100,
      }));
      setUploads(rehydrated);
      mergeUploadsIntoData(rehydrated);
    }
  }, []);

  // Persist filters and upload names. (Old persisted payloads may carry a
  // stray `sideOfBall` key from the retired global filter — harmlessly
  // ignored on load.)
  useEffect(() => {
    savePersisted({
      sessionType,
      selectedDate,
      uploadedNames: uploads.filter((u) => u.phase === "done").map((u) => u.filename),
    });
  }, [sessionType, selectedDate, uploads]);

  // Fetch live data when the API is configured and we are not in mock mode.
  useEffect(() => {
    if (mockMode) {
      setApiStatus("mock");
      return;
    }
    const baseUrl = process.env.NEXT_PUBLIC_API_URL;
    if (!baseUrl) {
      setApiStatus("offline");
      return;
    }
    let cancelled = false;
    setApiStatus("loading");
    const videoFilters: Record<string, string> = {};
    if (selectedDate) {
      videoFilters.recorded_after = `${selectedDate}T00:00:00Z`;
      videoFilters.recorded_before = `${selectedDate}T23:59:59.999999Z`;
    }
    if (sessionType !== "all") {
      videoFilters.session_kind = sessionType;
    }
    (async () => {
      try {
        // Typed api.ts clients with the auth token — the previous raw
        // fetch() calls here were the one unauthenticated code path left.
        const token = tokenRef.current;
        const [videosRes, jobsRes, scoutRes] = await Promise.allSettled([
          fetchVideos(videoFilters, token),
          fetchJobs({}, token),
          fetchSelfScoutTendencies(undefined, token),
        ]);
        if (cancelled) return;
        const anyFulfilled =
          videosRes.status === "fulfilled" ||
          jobsRes.status === "fulfilled" ||
          scoutRes.status === "fulfilled";
        if (!anyFulfilled) {
          const rejectedReasons = [videosRes, jobsRes, scoutRes]
            .filter((r): r is PromiseRejectedResult => r.status === "rejected")
            .map((r) => r.reason);
          const allNetworkFailures =
            rejectedReasons.length > 0 && rejectedReasons.every(looksNetworkFailure);
          // HTTP 4xx/5xx means the backend is reachable (not offline), even if
          // a specific surface is forbidden or currently erroring.
          setApiStatus(allNetworkFailures ? "offline" : "live");
          return;
        }
        setData((cur) => ({
          ...cur,
          videos: pickArrLive<ApiVideo>(videosRes, cur.videos),
          jobs: pickArrLive<ApiJob>(jobsRes, cur.jobs),
          selfScout: pickObjLive<SelfScoutResponse>(
            scoutRes,
            cur.selfScout,
            isSelfScoutResponse,
          ),
        }));
        setApiStatus("live");
      } catch (err) {
        if (!cancelled) setApiStatus(looksNetworkFailure(err) ? "offline" : "live");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [mockMode, selectedDate, sessionType, authToken]);

  // Fetch the active roster from /api/v1/players on mount and whenever
  // mockMode changes. Roster data is independent of the date/session filters
  // because the player table is roster-scoped, not film-scoped.
  useEffect(() => {
    if (mockMode) {
      setPlayersStatus("mock");
      return;
    }
    const baseUrl = process.env.NEXT_PUBLIC_API_URL;
    if (!baseUrl) {
      setPlayersStatus("offline");
      return;
    }
    let cancelled = false;
    setPlayersStatus("loading");
    (async () => {
      try {
        const apiPlayers = await fetchPlayers({ is_active: true }, tokenRef.current);
        if (cancelled) return;
        const summaries = apiPlayers.map(apiPlayerToSummary);
        setData((cur) => ({ ...cur, players: summaries }));
        setPlayersStatus("live");
      } catch (err) {
        // Keep "offline" only for genuine network/CORS failures.
        if (!cancelled) setPlayersStatus(looksNetworkFailure(err) ? "offline" : "live");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [mockMode]);

  // Fetch inbox status periodically in non-mock mode
  const refreshInbox = useCallback(() => {
    if (mockMode) return;
    const base = process.env.NEXT_PUBLIC_API_URL;
    if (!base) return;
    fetchInboxStatus(tokenRef.current, true)
      .then(setInboxItems)
      .catch(() => { /* silently degrade */ });
  }, [mockMode]);

  useEffect(() => {
    refreshInbox();
    const id = setInterval(refreshInbox, 15_000);
    return () => clearInterval(id);
  }, [refreshInbox]);

  function mergeUploadsIntoData(newUploads: UploadedClip[]) {
    setData((cur) => {
      const existing = new Set(cur.videos.map((v) => v.filename));
      const additions = newUploads.filter((u) => !existing.has(u.filename));
      if (!additions.length) return cur;

      const newVideos: ApiVideo[] = [
        ...cur.videos,
        ...additions.map((u) => ({
          id: u.videoId ?? u.id,
          filename: u.filename,
          status: "uploaded",
          duration_seconds: null,
          fps: null,
          width: null,
          height: null,
          created_at: u.uploadedAt,
        })),
      ];

      if (!mockMode) {
        return { ...cur, videos: newVideos };
      }

      const newClips: ClipSummary[] = [
        ...cur.clips,
        ...additions.map((u) => ({
          id: `clip-${u.id}`,
          title: u.filename.replace(/\.[^/.]+$/, ""),
          subtitle: "Newly uploaded film",
          duration: "00:12",
          tag: "Upload",
        })),
      ];

      const nextNum = cur.plays.length
        ? Math.max(...cur.plays.map((p) => p.number)) + 1
        : 1;
      const newPlays: PlaySummary[] = [
        ...cur.plays,
        ...additions.map((_, i) => ({
          number: nextNum + i,
          formation: "Trips Right",
          personnel: "11",
          concept: "Uploaded Clip",
          result: "Processed",
          yards: 6,
          confidence: 0.9,
        })),
      ];

      return { ...cur, videos: newVideos, clips: newClips, plays: newPlays };
    });
  }

  function updateUpload(id: string, patch: Partial<UploadedClip>) {
    setUploads((cur) => cur.map((u) => (u.id === id ? { ...u, ...patch } : u)));
  }

  async function executeUpload(clip: UploadedClip, file: File, metadata?: UploadMetadata) {
    const workerUrl = process.env.NEXT_PUBLIC_WORKER_URL;
    const apiUrl = process.env.NEXT_PUBLIC_API_URL;

    // In mock mode or when Worker/API is not configured, fall back to local-only
    if (mockMode || !workerUrl || !apiUrl) {
      updateUpload(clip.id, { phase: "done", progress: 100 });
      mergeUploadsIntoData([clip]);
      return;
    }

    try {
      // Step 1: Request upload URL from Worker
      updateUpload(clip.id, { phase: "requesting-url", progress: 0 });
      const token = await resolveToken();
      const { uploadUrl } = await requestUploadUrl(file.name, token);

      // Step 2: Upload file to R2 via Worker proxy
      updateUpload(clip.id, { phase: "uploading", progress: 0 });
      let r2Result;
      try {
        r2Result = await uploadToR2(uploadUrl, file, token, (loaded, total) => {
          const pct = Math.round((loaded / total) * 100);
          updateUpload(clip.id, { progress: pct });
        });
      } catch (err) {
        const msg = err instanceof Error ? err.message.toLowerCase() : String(err).toLowerCase();
        const tokenExpired = msg.includes("401") && (msg.includes("expired token") || msg.includes("invalid token"));
        if (!tokenExpired) throw err;

        const retriedToken = await resolveToken();
        if (!retriedToken || retriedToken === token) throw err;

        r2Result = await uploadToR2(uploadUrl, file, retriedToken, (loaded, total) => {
          const pct = Math.round((loaded / total) * 100);
          updateUpload(clip.id, { progress: pct });
        });
      }

      // Step 3: Register video with backend
      updateUpload(clip.id, { phase: "registering", progress: 100 });
      const registerToken = tokenRef.current;
      const video = await registerVideo({
        filename: file.name,
        storage_uri: r2Result.storageUri,
        recorded_at: metadata?.recorded_at,
        session_kind: metadata?.session_kind,
        source_type: metadata?.source_type,
        opponent_team: metadata?.opponent_team,
        our_possession: metadata?.our_possession,
      }, registerToken);

      updateUpload(clip.id, {
        phase: "done",
        progress: 100,
        storageUri: r2Result.storageUri,
        videoId: video.id,
      });
      mergeUploadsIntoData([{ ...clip, videoId: video.id }]);
      refreshInbox();
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      updateUpload(clip.id, { phase: "error", error: message });
    }
  }

  const addUploads = useCallback(async (files: FileList | File[], metadata?: UploadMetadata) => {
    const arr = Array.from(files);
    const created: UploadedClip[] = arr.map((f, i) => ({
      id: `up-${Date.now()}-${i}`,
      filename: f.name,
      objectUrl: typeof URL !== "undefined" ? URL.createObjectURL(f) : undefined,
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

    // Execute uploads concurrently
    for (let i = 0; i < created.length; i++) {
      executeUpload(created[i], arr[i], metadata);
    }

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

function pickObjLive<T>(
  r: PromiseSettledResult<unknown>,
  fallback: T,
  isExpectedShape: (value: unknown) => value is T,
): T {
  if (r.status === "fulfilled" && isExpectedShape(r.value)) {
    return r.value as T;
  }
  return fallback;
}

function isSelfScoutResponse(value: unknown): value is SelfScoutResponse {
  return (
    !!value &&
    typeof value === "object" &&
    !Array.isArray(value) &&
    "pre_snap_tells" in value &&
    Array.isArray((value as { pre_snap_tells: unknown }).pre_snap_tells)
  );
}
