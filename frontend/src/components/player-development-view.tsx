"use client";

/**
 * Player Development.
 *
 * Real surfaces only: the selected player's identity focus, the
 * body-orientation proxy + effort review candidates from /api/v1/metrics, and
 * the coach corrections flow (POST /api/v1/corrections). The old hardcoded
 * "Best Teaching Clips" mock grid was removed (#96).
 */

import Link from "next/link";
import { CheckCircle2, Pause, UserRound } from "lucide-react";
import { useEffect, useState } from "react";
import { useAppState } from "@/lib/app-state";
import { createCorrection, fetchMetrics, type ApiMetric } from "@/lib/api";
import { PlayerPortrait } from "@/components/shared/player-portrait";
import { TrendLine } from "@/components/shared/trend-line";
import { PlayerFocus, playerProfileHref } from "@/components/players-view";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { EmptyState } from "@/components/composite/empty-state";
import { StatLine } from "@/components/composite/stat-chip";

export function PlayerDevelopmentView() {
  const { data, selectedPlayer, setSelectedPlayerId, authToken } = useAppState();
  const pool = data.players;
  const [developmentMetrics, setDevelopmentMetrics] = useState<ApiMetric[]>([]);
  const [metricsState, setMetricsState] = useState<"idle" | "loading" | "ready" | "error">("idle");
  const [metricsError, setMetricsError] = useState<string | null>(null);
  const [correctionState, setCorrectionState] = useState<Record<string, "saving" | "saved" | "error">>({});
  const selectedPlayerId = selectedPlayer?.id;

  useEffect(() => {
    if (!authToken || !selectedPlayerId) {
      setDevelopmentMetrics([]);
      setMetricsState("idle");
      setMetricsError(null);
      return;
    }
    let cancelled = false;
    setMetricsState("loading");
    setMetricsError(null);
    Promise.all([
      fetchMetrics({ metric_name: "effort_review_candidate", player_id: selectedPlayerId, limit: 200 }, authToken),
      fetchMetrics({ metric_name: "pose_body_orientation_proxy", player_id: selectedPlayerId, limit: 200 }, authToken),
    ])
      .then(([effort, orientation]) => {
        if (cancelled) return;
        setDevelopmentMetrics([...effort, ...orientation]);
        setMetricsState("ready");
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setDevelopmentMetrics([]);
        setMetricsError(err instanceof Error ? err.message : String(err));
        setMetricsState("error");
      });
    return () => {
      cancelled = true;
    };
  }, [authToken, selectedPlayerId]);

  if (!selectedPlayer) {
    return (
      <EmptyState
        icon={UserRound}
        title="No players yet"
        hint="Player development surfaces appear once the roster has players."
      />
    );
  }

  const playerMetrics = developmentMetrics.filter(
    (metric) => metric.metric_value.player_id === selectedPlayer.id,
  );
  const effortMetrics = playerMetrics.filter((metric) => metric.metric_name === "effort_review_candidate");
  const orientationMetrics = playerMetrics.filter(
    (metric) => metric.metric_name === "pose_body_orientation_proxy",
  );

  const submitEffortCorrection = async (metric: ApiMetric, loafFlag: boolean) => {
    if (!authToken) return;
    setCorrectionState((cur) => ({ ...cur, [metric.id]: "saving" }));
    try {
      await createCorrection(
        {
          clip_id: metric.clip_id,
          correction_type: "effort_tag",
          original_value: { metric_id: metric.id, ...metric.metric_value },
          corrected_value: {
            metric_id: metric.id,
            player_id: selectedPlayer.id,
            loaf_flag: loafFlag,
            review_state: loafFlag ? "coach_confirmed" : "coach_cleared",
          },
          training_eligible: true,
        },
        authToken,
      );
      setCorrectionState((cur) => ({ ...cur, [metric.id]: "saved" }));
    } catch {
      setCorrectionState((cur) => ({ ...cur, [metric.id]: "error" }));
    }
  };

  return (
    <div className="flex flex-col gap-4">
      <div className="grid gap-4 lg:grid-cols-3">
        <Card>
          <CardContent>
            <PlayerFocus player={selectedPlayer} allPlayers={pool} onSelect={setSelectedPlayerId} />
            <Button asChild className="mt-4 w-full">
              <Link href={playerProfileHref(selectedPlayer.id)}>
                <UserRound className="size-4" /> Open Full Profile
              </Link>
            </Button>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <h2 className="font-display text-base font-semibold uppercase tracking-wide">
              Body-Orientation Proxy
            </h2>
          </CardHeader>
          <CardContent>
            <PlayerPortrait player={selectedPlayer} compact />
            <DevelopmentMetricState
              authToken={authToken}
              state={metricsState}
              error={metricsError}
              empty={orientationMetrics.length === 0}
              emptyMessage="No body-orientation review candidates for this player."
            />
            {orientationMetrics.slice(0, 3).map((metric) => (
              <BodyOrientationRow key={metric.id} metric={metric} />
            ))}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <h2 className="font-display text-base font-semibold uppercase tracking-wide">
              Trend Lines
            </h2>
          </CardHeader>
          <CardContent>
            <TrendLine data={selectedPlayer.trend} />
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <h2 className="font-display text-base font-semibold uppercase tracking-wide">
            Effort Review Candidates
          </h2>
        </CardHeader>
        <CardContent className="flex flex-col gap-2">
          <DevelopmentMetricState
            authToken={authToken}
            state={metricsState}
            error={metricsError}
            empty={effortMetrics.length === 0}
            emptyMessage="No effort review candidates for this player."
          />
          {effortMetrics.slice(0, 6).map((metric) => (
            <EffortReviewRow
              key={metric.id}
              metric={metric}
              status={correctionState[metric.id]}
              onConfirm={() => submitEffortCorrection(metric, true)}
              onClear={() => submitEffortCorrection(metric, false)}
            />
          ))}
        </CardContent>
      </Card>
    </div>
  );
}

function DevelopmentMetricState({
  authToken,
  state,
  error,
  empty,
  emptyMessage,
}: {
  authToken?: string;
  state: "idle" | "loading" | "ready" | "error";
  error: string | null;
  empty: boolean;
  emptyMessage: string;
}) {
  if (!authToken) {
    return (
      <p className="mt-2 text-xs text-muted-foreground">
        Sign in to view live review candidates.
      </p>
    );
  }
  if (state === "loading") {
    return <p className="mt-2 text-xs text-muted-foreground">Loading review candidates…</p>;
  }
  if (state === "error") {
    return (
      <p className="mt-2 text-xs text-muted-foreground">
        {error ?? "Review candidates unavailable."}
      </p>
    );
  }
  if (state === "ready" && empty) {
    return <p className="mt-2 text-xs text-muted-foreground">{emptyMessage}</p>;
  }
  return null;
}

function EffortReviewRow({
  metric,
  status,
  onConfirm,
  onClear,
}: {
  metric: ApiMetric;
  status?: "saving" | "saved" | "error";
  onConfirm: () => void;
  onClear: () => void;
}) {
  const value = metric.metric_value;
  const flagged = metric.loaf_flag === true || value.loaf_flag === true;
  const reasonCodes = Array.isArray(value.reason_codes) ? value.reason_codes.join(", ") : "review";
  return (
    <div className="flex items-center justify-between gap-3 border-b border-border-soft pb-2 last:border-b-0 last:pb-0">
      <div className="min-w-0">
        <span className="text-[0.85rem] font-semibold">
          {flagged ? "Possible effort drop" : "Effort range check"}
        </span>
        <div data-numeric className="mt-0.5 font-mono text-xs text-muted-foreground">
          z {formatMaybeNumber(metric.effort_zscore)} · confidence{" "}
          {formatMaybePct(metric.confidence)} · {reasonCodes}
        </div>
      </div>
      <div className="flex shrink-0 items-center gap-1.5">
        <Button
          variant="outline"
          size="icon"
          className="size-8"
          aria-label="Confirm effort review candidate"
          onClick={onConfirm}
        >
          <CheckCircle2 className="size-4" />
        </Button>
        <Button
          variant="outline"
          size="icon"
          className="size-8"
          aria-label="Clear effort review candidate"
          onClick={onClear}
        >
          <Pause className="size-4" />
        </Button>
        {status === "saving" && <span className="text-xs text-muted-foreground">Saving</span>}
        {status === "saved" && <span className="text-xs text-status-ok">Saved</span>}
        {status === "error" && <span className="text-xs text-status-danger">Error</span>}
      </div>
    </div>
  );
}

function BodyOrientationRow({ metric }: { metric: ApiMetric }) {
  const value = metric.metric_value;
  return (
    <div className="mt-2 flex flex-col gap-1">
      <StatLine label="Proxy class" value={orientationLabel(value.orientation_class)} />
      <StatLine label="Head yaw" value={`${formatMaybeNumber(value.head_yaw_deg)}°`} />
      <StatLine label="Confidence" value={formatMaybePct(metric.confidence)} />
      <StatLine label="Review state" value={String(value.review_state ?? "needs review")} />
    </div>
  );
}

function formatMaybeNumber(value: unknown): string {
  return typeof value === "number" && Number.isFinite(value) ? String(Math.round(value * 10) / 10) : "—";
}

function formatMaybePct(value: unknown): string {
  return typeof value === "number" && Number.isFinite(value) ? `${Math.round(value * 100)}%` : "—";
}

function orientationLabel(value: unknown): string {
  switch (value) {
    case "body_inside":
      return "Inside";
    case "body_on_receiver":
      return "Receiver";
    case "body_backfield":
      return "Backfield";
    default:
      return "Needs review";
  }
}
