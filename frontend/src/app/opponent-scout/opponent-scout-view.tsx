"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { AnalyticsCard, type AnalyticsCardState } from "@/components/analytics-card";
import { TendencyTable } from "@/components/shared/tendency-table";
import { useAppState } from "@/lib/app-state";
import type { FetchState } from "@/lib/fetch-state";
import {
  fetchOpponentTendencies,
  fetchOpponents,
} from "@/lib/api";
import type {
  OpponentSummary,
  OpponentVideo,
  SelfScoutResponse,
} from "@/lib/types";
import { Card, CardHeader } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { NativeSelect } from "@/components/ui/native-select";
import { StatusBadge } from "@/components/composite/status-badge";

type OpponentListState = FetchState<OpponentSummary[]>;

// Adds "idle" (no film selected yet) on top of the shared fetch lifecycle.
type TendencyState = { kind: "idle" } | FetchState<SelfScoutResponse>;

const OFFLINE_REASON =
  "Opponent Scout is unavailable offline — tendencies appear when the team server is connected.";

export function OpponentScoutView() {
  const { authToken, mockMode } = useAppState();
  const [opponentList, setOpponentList] = useState<OpponentListState>({
    kind: "loading",
  });
  const [selectedOpponent, setSelectedOpponent] = useState<string>("");
  const [selectedVideo, setSelectedVideo] = useState<string>("");
  const [tendencyState, setTendencyState] = useState<TendencyState>({
    kind: "idle",
  });

  const loadOpponents = useCallback(async () => {
    const baseUrl = process.env.NEXT_PUBLIC_API_URL;
    if (!baseUrl) {
      setOpponentList({ kind: "offline" });
      return;
    }
    setOpponentList({ kind: "loading" });
    try {
      const opponents = await fetchOpponents(authToken);
      if (opponents.length === 0) {
        setOpponentList({ kind: "empty" });
      } else {
        setOpponentList({ kind: "ready", data: opponents });
      }
    } catch (err) {
      setOpponentList({
        kind: "error",
        message: err instanceof Error ? err.message : String(err),
      });
    }
  }, [authToken]);

  useEffect(() => {
    loadOpponents();
  }, [loadOpponents]);

  const currentOpponent = useMemo<OpponentSummary | null>(() => {
    if (opponentList.kind !== "ready") return null;
    if (!selectedOpponent) return null;
    return (
      opponentList.data.find((o) => o.opponent_team === selectedOpponent) ?? null
    );
  }, [opponentList, selectedOpponent]);

  // When the opponent changes, default the video selection to the most recent
  // film for that opponent (the backend returns videos newest-first).
  useEffect(() => {
    if (!currentOpponent) {
      setSelectedVideo("");
      return;
    }
    const first = currentOpponent.videos[0];
    setSelectedVideo(first ? first.video_id : "");
  }, [currentOpponent]);

  const loadTendencies = useCallback(async () => {
    if (!selectedVideo) {
      setTendencyState({ kind: "idle" });
      return;
    }
    setTendencyState({ kind: "loading" });
    try {
      const data = await fetchOpponentTendencies(selectedVideo, authToken);
      if (data.clip_count === 0) {
        setTendencyState({ kind: "empty" });
      } else {
        setTendencyState({ kind: "ready", data });
      }
    } catch (err) {
      setTendencyState({
        kind: "error",
        message: err instanceof Error ? err.message : String(err),
      });
    }
  }, [selectedVideo, authToken]);

  useEffect(() => {
    loadTendencies();
  }, [loadTendencies]);

  const cardState = useMemo<AnalyticsCardState>(() => {
    if (opponentList.kind === "offline") {
      return { kind: "unavailable", reason: OFFLINE_REASON };
    }
    if (opponentList.kind === "error") {
      return {
        kind: "error",
        message: opponentList.message,
        onRetry: loadOpponents,
      };
    }
    if (opponentList.kind === "empty") {
      return {
        kind: "empty",
        reason:
          "No opponent film uploaded yet. Upload a game-tagged video with an opponent team to populate this picker.",
      };
    }
    if (!selectedOpponent) {
      return {
        kind: "empty",
        reason: "Pick an opponent above to load tendencies.",
      };
    }
    if (!selectedVideo) {
      return {
        kind: "empty",
        reason:
          "This opponent has no scoutable film. Upload an opponent cutup tagged to this team.",
      };
    }
    switch (tendencyState.kind) {
      case "idle":
      case "loading":
        return { kind: "loading", label: "Computing opponent tendencies…" };
      case "offline":
        return { kind: "unavailable", reason: OFFLINE_REASON };
      case "empty":
        return {
          kind: "empty",
          reason:
            "Opponent film is uploaded but no labeled plays are available yet. The labeling pipeline may still be running.",
        };
      case "error":
        return {
          kind: "error",
          message: tendencyState.message,
          onRetry: loadTendencies,
        };
      case "ready":
        return { kind: "live" };
    }
  }, [opponentList, selectedOpponent, selectedVideo, tendencyState, loadOpponents, loadTendencies]);

  const data = tendencyState.kind === "ready" ? tendencyState.data : null;

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHeader className="flex flex-row flex-wrap items-start justify-between gap-3">
          <div>
            <h2 className="font-display text-base font-semibold uppercase tracking-wide">
              Opponent picker
            </h2>
            <p className="mt-0.5 max-w-2xl text-xs text-muted-foreground">
              Game-plan against a specific opponent. Opponents are derived from uploaded game film
              tagged with a team name.
              {mockMode ? " Mock mode shows whatever the backend returns when configured." : ""}
            </p>
            {opponentList.kind === "ready" && opponentList.data.length > 0 && (
              <p className="mt-1 text-xs text-muted-foreground">
                {opponentList.data.length} opponent
                {opponentList.data.length === 1 ? "" : "s"} loaded.
              </p>
            )}
          </div>
          <div className="flex flex-wrap gap-3">
            <OpponentPicker
              state={opponentList}
              selected={selectedOpponent}
              onChange={setSelectedOpponent}
            />
            <VideoPicker
              opponent={currentOpponent}
              selected={selectedVideo}
              onChange={setSelectedVideo}
            />
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
                  data-testid={`opp-pre-snap-tell-${tell.grouping_key}`}
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
    </div>
  );
}

function OpponentPicker({
  state,
  selected,
  onChange,
}: {
  state: OpponentListState;
  selected: string;
  onChange: (value: string) => void;
}) {
  const disabled = state.kind !== "ready";
  return (
    <div className="flex min-w-56 flex-col gap-1">
      <Label
        htmlFor="opponent-picker"
        className="font-display text-[0.68rem] font-semibold uppercase tracking-widest text-muted-foreground"
      >
        Opponent
      </Label>
      <NativeSelect
        id="opponent-picker"
        value={selected}
        onChange={(e) => onChange(e.target.value)}
        disabled={disabled}
        aria-label="Opponent"
        data-testid="opponent-picker"
      >
        <option value="">
          {state.kind === "loading"
            ? "Loading opponents…"
            : state.kind === "empty"
              ? "No opponent film yet"
              : state.kind === "offline"
                ? "Backend offline"
                : state.kind === "error"
                  ? "Could not load opponents"
                  : "Select an opponent"}
        </option>
        {state.kind === "ready" &&
          state.data.map((o) => (
            <option key={o.opponent_team} value={o.opponent_team}>
              {o.opponent_team} ({o.video_count} video{o.video_count === 1 ? "" : "s"})
            </option>
          ))}
      </NativeSelect>
    </div>
  );
}

function VideoPicker({
  opponent,
  selected,
  onChange,
}: {
  opponent: OpponentSummary | null;
  selected: string;
  onChange: (value: string) => void;
}) {
  const videos = opponent?.videos ?? [];
  return (
    <div className="flex min-w-64 flex-col gap-1">
      <Label
        htmlFor="opponent-video-picker"
        className="font-display text-[0.68rem] font-semibold uppercase tracking-widest text-muted-foreground"
      >
        Opponent film
      </Label>
      <NativeSelect
        id="opponent-video-picker"
        value={selected}
        onChange={(e) => onChange(e.target.value)}
        disabled={!opponent || videos.length === 0}
        aria-label="Opponent film"
        data-testid="opponent-video-picker"
      >
        <option value="">
          {!opponent
            ? "Pick an opponent first"
            : videos.length === 0
              ? "No film uploaded"
              : "Select film"}
        </option>
        {videos.map((v) => (
          <option key={v.video_id} value={v.video_id}>
            {opponentVideoLabel(v)}
          </option>
        ))}
      </NativeSelect>
    </div>
  );
}

function opponentVideoLabel(v: OpponentVideo): string {
  const date = v.recorded_at ? v.recorded_at.slice(0, 10) : v.created_at.slice(0, 10);
  return `${date} · ${v.filename}`;
}
