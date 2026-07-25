"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { AnalyticsCard, type AnalyticsCardState } from "@/components/analytics-card";
import { TendencyTable } from "@/components/shared/tendency-table";
import { useAppState } from "@/lib/app-state";
import type { FetchState } from "@/lib/fetch-state";
import {
  actionAlert,
  fetchSelfScoutTendencies,
  fetchTendencyBreakAlerts,
  fetchVideos,
  generateTendencyBreak,
  type TendencyBreakAlert,
  type VideoFilters,
} from "@/lib/api";
import type { ApiVideo, SelfScoutResponse } from "@/lib/types";
import { ExperimentalBadge } from "@/components/experimental-badge";
import { Button } from "@/components/ui/button";
import { Card, CardHeader } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { NativeSelect } from "@/components/ui/native-select";
import { StatusBadge } from "@/components/composite/status-badge";

type ScoutDataState = FetchState<SelfScoutResponse>;

type BreakState = FetchState<TendencyBreakAlert[]>;

const ALL_VIDEOS = "__all__";

const OFFLINE_REASON =
  "Self-Scout is unavailable offline — tendencies appear when the team server is connected.";

export function SelfScoutView() {
  const { selectedDate, sessionType, authToken, mockMode } = useAppState();
  const [videos, setVideos] = useState<ApiVideo[]>([]);
  const [videosError, setVideosError] = useState<string | null>(null);
  const [selectedVideoId, setSelectedVideoId] = useState<string>(ALL_VIDEOS);
  const [state, setState] = useState<ScoutDataState>({ kind: "loading" });

  // Build the source film picker. Self-scout sources are our own film:
  // practice and scrimmage (game film with our possession is also valid, but
  // most coaches scout opponent-facing tendencies on practice). We expose any
  // video that isn't tagged with an opponent — `opponent_team is null` is the
  // closest server-side proxy for "Toledo film" today.
  useEffect(() => {
    const baseUrl = process.env.NEXT_PUBLIC_API_URL;
    if (!baseUrl) {
      setVideos([]);
      setVideosError(null);
      return;
    }
    let cancelled = false;
    const filters: VideoFilters = { limit: 200 };
    if (selectedDate) {
      filters.recorded_after = `${selectedDate}T00:00:00Z`;
      filters.recorded_before = `${selectedDate}T23:59:59.999999Z`;
    }
    if (sessionType !== "all") {
      filters.session_kind = sessionType;
    }
    fetchVideos(filters, authToken)
      .then((all) => {
        if (cancelled) return;
        // Self-scout = our film. Drop entries that are tagged with an
        // opponent (those belong on the Opponent Prep tab).
        const ourFilm = all.filter((v) => !v.opponent_team);
        setVideos(ourFilm);
        setVideosError(null);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setVideos([]);
        setVideosError(err instanceof Error ? err.message : String(err));
      });
    return () => {
      cancelled = true;
    };
  }, [selectedDate, sessionType, authToken]);

  useEffect(() => {
    if (selectedVideoId !== ALL_VIDEOS && !videos.some((v) => v.id === selectedVideoId)) {
      setSelectedVideoId(ALL_VIDEOS);
    }
  }, [selectedVideoId, videos]);

  const loadTendencies = useCallback(async () => {
    const baseUrl = process.env.NEXT_PUBLIC_API_URL;
    if (!baseUrl) {
      setState({ kind: "offline" });
      return;
    }
    setState({ kind: "loading" });
    try {
      const videoId = selectedVideoId === ALL_VIDEOS ? null : selectedVideoId;
      const data = await fetchSelfScoutTendencies(videoId, authToken);
      if (data.clip_count === 0) {
        setState({ kind: "empty" });
      } else {
        setState({ kind: "ready", data });
      }
    } catch (err) {
      setState({
        kind: "error",
        message: err instanceof Error ? err.message : String(err),
      });
    }
  }, [selectedVideoId, authToken]);

  useEffect(() => {
    loadTendencies();
  }, [loadTendencies]);

  // ── Tendency-break alerts (Issue #137) ──────────────────────────────────────
  const [breakState, setBreakState] = useState<BreakState>({ kind: "loading" });
  const [generating, setGenerating] = useState(false);

  const loadBreaks = useCallback(async () => {
    const baseUrl = process.env.NEXT_PUBLIC_API_URL;
    if (!baseUrl) {
      setBreakState({ kind: "offline" });
      return;
    }
    setBreakState({ kind: "loading" });
    try {
      const res = await fetchTendencyBreakAlerts({ limit: 100 }, authToken);
      const list = Array.isArray(res?.alerts) ? res.alerts : [];
      setBreakState(list.length === 0 ? { kind: "empty" } : { kind: "ready", data: list });
    } catch (err) {
      setBreakState({
        kind: "error",
        message: err instanceof Error ? err.message : String(err),
      });
    }
  }, [authToken]);

  useEffect(() => {
    loadBreaks();
  }, [loadBreaks]);

  const handleGenerate = useCallback(async () => {
    setGenerating(true);
    try {
      const videoId = selectedVideoId === ALL_VIDEOS ? undefined : selectedVideoId;
      await generateTendencyBreak({ game_video_id: videoId }, authToken);
      await loadBreaks();
    } catch (err) {
      setBreakState({
        kind: "error",
        message: err instanceof Error ? err.message : String(err),
      });
    } finally {
      setGenerating(false);
    }
  }, [authToken, selectedVideoId, loadBreaks]);

  const handleActionBreak = useCallback(
    async (alertId: string) => {
      try {
        await actionAlert(alertId, authToken);
        setBreakState((cur) => {
          if (cur.kind !== "ready") return cur;
          return {
            kind: "ready",
            data: cur.data.map((a) =>
              a.id === alertId ? { ...a, is_actioned: true } : a,
            ),
          };
        });
      } catch {
        // Leave the alert open on failure.
      }
    },
    [authToken],
  );

  const breakCardState = useMemo<AnalyticsCardState>(() => {
    switch (breakState.kind) {
      case "loading":
        return { kind: "loading", label: "Loading tendency breaks…" };
      case "offline":
        return {
          kind: "unavailable",
          reason:
            "Tendency-break alerts are unavailable offline — they appear when the team server is connected.",
        };
      case "empty":
        return {
          kind: "empty",
          reason:
            "No tendency-break alerts yet. Generate them from your labeled film with the button above.",
        };
      case "error":
        return { kind: "error", message: breakState.message, onRetry: loadBreaks };
      case "ready":
        return { kind: "live" };
    }
  }, [breakState, loadBreaks]);

  const cardState = useMemo<AnalyticsCardState>(() => {
    switch (state.kind) {
      case "loading":
        return { kind: "loading", label: "Computing Self-Scout tendencies…" };
      case "offline":
        return { kind: "unavailable", reason: OFFLINE_REASON };
      case "empty":
        return {
          kind: "empty",
          reason:
            "No labeled plays match the selected film yet. Upload practice film or wait for the labeling pipeline to finish.",
        };
      case "error":
        return { kind: "error", message: state.message, onRetry: loadTendencies };
      case "ready":
        return { kind: "live" };
    }
  }, [state, loadTendencies]);

  const data = state.kind === "ready" ? state.data : null;

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHeader className="flex flex-row flex-wrap items-start justify-between gap-3">
          <div>
            <h2 className="font-display text-base font-semibold uppercase tracking-wide">
              Self-Scout filter
            </h2>
            <p className="mt-0.5 max-w-2xl text-xs text-muted-foreground">
              Pick a single piece of our film to scout, or scout across every uploaded session.
              Date and Session Type filters also narrow the picker.
              {mockMode ? " Mock mode shows whatever the backend returns when configured." : ""}
            </p>
            {videosError && (
              <p className="mt-2 text-xs text-status-danger" data-testid="self-scout-videos-error">
                Could not load source film list: {videosError}
              </p>
            )}
          </div>
          <div className="flex min-w-72 flex-col gap-1">
            <Label
              htmlFor="self-scout-video-picker"
              className="font-display text-[0.68rem] font-semibold uppercase tracking-widest text-muted-foreground"
            >
              Source film
            </Label>
            <NativeSelect
              id="self-scout-video-picker"
              value={selectedVideoId}
              onChange={(e) => setSelectedVideoId(e.target.value)}
              aria-label="Self-Scout source video"
              data-testid="self-scout-video-picker"
            >
              <option value={ALL_VIDEOS}>All my film</option>
              {videos.map((v) => (
                <option key={v.id} value={v.id}>
                  {videoLabel(v)}
                </option>
              ))}
            </NativeSelect>
          </div>
        </CardHeader>
      </Card>

      <div className="grid gap-4 lg:grid-cols-2">
        <AnalyticsCard title="Run / Pass by Formation" state={cardState}>
          {data && <TendencyTable entries={data.formation_tendencies} />}
        </AnalyticsCard>

        <AnalyticsCard title="Personnel Tendencies" state={cardState}>
          {data && <TendencyTable entries={data.personnel_tendencies} />}
        </AnalyticsCard>

        <AnalyticsCard title="Motion Split" state={cardState}>
          {data && (
            <div className="flex flex-col gap-1.5">
              <MotionRow label="With motion" split={data.motion_tendencies.with_motion} />
              <MotionRow label="Without motion" split={data.motion_tendencies.without_motion} />
            </div>
          )}
        </AnalyticsCard>

        <AnalyticsCard title="Down & Distance" state={cardState}>
          {data && (
            <TendencyTable
              entries={data.down_distance_tendencies.map((e) => ({
                ...e,
                grouping_key: `${ordinalDown(e.down)} · ${distanceLabel(e.distance_bucket)}`,
              }))}
            />
          )}
        </AnalyticsCard>
      </div>

      <AnalyticsCard title="Pre-Snap Tells" state={cardState}>
        {data &&
          (data.pre_snap_tells.length === 0 ? (
            <p className="text-xs text-muted-foreground">
              No exposure leans crossed the alert threshold for this film.
            </p>
          ) : (
            <div className="flex flex-col gap-2">
              {data.pre_snap_tells.map((tell) => (
                <div
                  key={tell.grouping_key}
                  className="flex items-start justify-between gap-3 border-b border-border-soft pb-2 last:border-b-0 last:pb-0"
                  data-testid={`pre-snap-tell-${tell.grouping_key}`}
                >
                  <div className="min-w-0">
                    <span className="text-[0.85rem] font-semibold">{tell.formation}</span>
                    <div className="mt-0.5 text-xs text-muted-foreground">{tell.message}</div>
                  </div>
                  <StatusBadge
                    tone={tell.severity === "high" ? "danger" : "warn"}
                    className="capitalize"
                  >
                    {tell.severity}
                  </StatusBadge>
                </div>
              ))}
            </div>
          ))}
      </AnalyticsCard>

      <AnalyticsCard
        title="Tendency-Break Alerts"
        state={breakCardState}
        headerExtra={
          <Button
            size="sm"
            data-testid="generate-tendency-break"
            onClick={handleGenerate}
            disabled={generating}
          >
            {generating ? "Generating…" : "Generate from film"}
          </Button>
        }
      >
        {breakState.kind === "ready" && (
          <TendencyBreakList alerts={breakState.data} onAction={handleActionBreak} />
        )}
      </AnalyticsCard>
    </div>
  );
}

function TendencyBreakList({
  alerts,
  onAction,
}: {
  alerts: TendencyBreakAlert[];
  onAction: (alertId: string) => void;
}) {
  return (
    <div className="flex flex-col gap-3">
      {alerts.map((a) => (
        <div
          key={a.id ?? a.grouping_key}
          className="flex items-start justify-between gap-3 border-b border-border-soft pb-3 last:border-b-0 last:pb-0"
          data-testid={`tendency-break-${a.id ?? a.grouping_key}`}
        >
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-[0.85rem] font-semibold">{a.grouping_key}</span>
              {a.tendency_kind === "pattern_break" && <ExperimentalBadge label="Pattern break" />}
            </div>
            <div className="mt-0.5 text-xs text-muted-foreground">{a.message}</div>
            <div data-numeric className="mt-0.5 font-mono text-xs text-muted-foreground">
              {Math.round(a.pass_rate * 100)}% pass · {Math.round(a.run_rate * 100)}% run ·{" "}
              {a.total_plays} plays · source {a.source}
              {a.is_actioned ? " · actioned" : ""}
            </div>
          </div>
          <div className="flex shrink-0 flex-col items-end gap-1.5">
            <StatusBadge tone={a.severity === "high" ? "danger" : "warn"} className="capitalize">
              {a.severity}
            </StatusBadge>
            {a.id && !a.is_actioned && (
              <Button
                variant="outline"
                size="sm"
                data-testid={`action-break-${a.id}`}
                onClick={() => onAction(a.id as string)}
              >
                Mark actioned
              </Button>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}

function MotionRow({
  label,
  split,
}: {
  label: string;
  split: { total: number; run_rate: number; pass_rate: number };
}) {
  return (
    <div className="grid grid-cols-[1fr_auto_auto_auto] items-baseline gap-3 text-[0.82rem]">
      <span className="font-medium">{label}</span>
      <span data-numeric className="font-mono text-xs text-muted-foreground">
        {split.total} plays
      </span>
      <span data-numeric className="font-mono text-xs text-muted-foreground">
        {(split.run_rate * 100).toFixed(0)}% run
      </span>
      <span data-numeric className="font-mono text-xs text-muted-foreground">
        {(split.pass_rate * 100).toFixed(0)}% pass
      </span>
    </div>
  );
}

function videoLabel(v: ApiVideo): string {
  const date = v.recorded_at ? v.recorded_at.slice(0, 10) : v.created_at.slice(0, 10);
  const kind = v.session_kind ?? "session";
  return `${date} · ${kind} · ${v.filename}`;
}

function ordinalDown(down: number): string {
  switch (down) {
    case 1:
      return "1st";
    case 2:
      return "2nd";
    case 3:
      return "3rd";
    case 4:
      return "4th";
    default:
      return `${down}th`;
  }
}

function distanceLabel(bucket: string): string {
  switch (bucket) {
    case "short":
      return "Short (1-3)";
    case "medium":
      return "Medium (4-6)";
    case "long":
      return "Long (7+)";
    default:
      return bucket;
  }
}
