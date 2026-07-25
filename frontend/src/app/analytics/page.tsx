"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { FootballShell } from "@/components/shell/app-shell";
import { AnalyticsCard, type AnalyticsCardState } from "@/components/analytics-card";
import { TendencyTable } from "@/components/shared/tendency-table";
import { useAppState } from "@/lib/app-state";
import { useFetchState, type FetchState } from "@/lib/fetch-state";
import {
  fetchFrontierMetrics,
  fetchSelfScoutTendencies,
  fetchVideos,
  type FrontierMetric,
} from "@/lib/api";
import type { SelfScoutResponse } from "@/lib/types";
import { ExperimentalBadge } from "@/components/experimental-badge";
import { StatChip } from "@/components/composite/stat-chip";
import { FilterBar } from "@/components/composite/filter-bar";
import { Card, CardContent } from "@/components/ui/card";

const OFFLINE_REASON =
  "Backend offline — live metrics appear when the team server is connected.";

const FRONTIER_UNAVAILABLE_REASON: Record<string, string> = {
  xsep: "xSep requires calibrated receiver tracking (#127/#128/#129). No experimental samples yet for this filter.",
  xyards:
    "xYards requires the play-outcome metrics pipeline. No experimental samples yet for this filter.",
  xpressure:
    "xPressure requires pass-rush tracking + snap/throw events. No experimental samples yet for this filter.",
};

export default function AnalyticsPage() {
  return (
    <FootballShell activePage="analytics">
      <AnalyticsView />
    </FootballShell>
  );
}

function AnalyticsView() {
  const { authToken, selectedDate, sessionType } = useAppState();
  // Frontier analytics (Issue #10) — experimental, may be empty.
  const [frontier, setFrontier] = useState<FrontierMetric[] | null>(null);
  const [frontierLoading, setFrontierLoading] = useState(true);

  const videosFetcher = useCallback(() => {
    const filters: Record<string, string | number> = { limit: 200 };
    if (selectedDate) {
      filters.recorded_after = `${selectedDate}T00:00:00Z`;
      filters.recorded_before = `${selectedDate}T23:59:59.999999Z`;
    }
    if (sessionType !== "all") {
      filters.session_kind = sessionType;
    }
    return fetchVideos(filters, authToken);
  }, [authToken, selectedDate, sessionType]);
  const { state: videos, reload: loadVideos } = useFetchState(videosFetcher);

  const scoutFetcher = useCallback(
    () => fetchSelfScoutTendencies(null, authToken),
    [authToken],
  );
  const { state: scout, reload: loadScout } = useFetchState(scoutFetcher, {
    isEmpty: (data) => data.clip_count === 0,
  });

  const loadFrontier = useCallback(async () => {
    const baseUrl = process.env.NEXT_PUBLIC_API_URL;
    if (!baseUrl) {
      setFrontier(null);
      setFrontierLoading(false);
      return;
    }
    setFrontierLoading(true);
    try {
      const res = await fetchFrontierMetrics({ limit: 100 }, authToken);
      setFrontier(Array.isArray(res?.metrics) ? res.metrics : []);
    } catch {
      // Experimental scaffolds are expected to be absent — fall back to the
      // "unavailable" card state rather than surfacing a hard error.
      setFrontier(null);
    } finally {
      setFrontierLoading(false);
    }
  }, [authToken]);

  useEffect(() => {
    loadFrontier();
  }, [loadFrontier]);

  const totalPlaysState = useMemo<AnalyticsCardState>(() => {
    switch (videos.kind) {
      case "loading":
        return { kind: "loading" };
      case "offline":
        return { kind: "unavailable", reason: OFFLINE_REASON };
      case "error":
        return { kind: "error", message: videos.message, onRetry: loadVideos };
      case "empty":
        return {
          kind: "empty",
          reason: "No videos uploaded for the selected date / session yet.",
        };
      case "ready":
        return { kind: "live" };
    }
  }, [videos, loadVideos]);

  const scoutCardState = scoutToCardState(scout, loadScout);

  return (
    <div className="flex flex-col gap-4">
      <FilterBar className="mb-0" />

      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
        <AnalyticsCard title="Film Volume" state={totalPlaysState}>
          {videos.kind === "ready" && (
            <div className="grid grid-cols-3 gap-2">
              <StatChip label="Videos" value={String(videos.data.length)} />
              <StatChip
                label="Ready"
                value={String(videos.data.filter((v) => v.status === "ready").length)}
              />
              <StatChip
                label="Processing"
                value={String(videos.data.filter((v) => v.status === "processing").length)}
              />
            </div>
          )}
        </AnalyticsCard>

        <FrontierCard
          title="Expected Separation (xSep)"
          metricName="xsep"
          valueKey="yards"
          unit="yd"
          metrics={frontier}
          loading={frontierLoading}
        />

        <FrontierCard
          title="Expected Yards (xYards)"
          metricName="xyards"
          valueKey="observed_yac_yd"
          unit="yd"
          metrics={frontier}
          loading={frontierLoading}
        />

        <FrontierCard
          title="Expected Pressure (xPressure)"
          metricName="xpressure"
          valueKey="xpressure"
          unit=""
          metrics={frontier}
          loading={frontierLoading}
        />
      </div>

      <AnalyticsCard title="Formation Run / Pass" state={scoutCardState}>
        {scout.kind === "ready" && <TendencyTable entries={scout.data.formation_tendencies} />}
      </AnalyticsCard>

      {/* Capabilities that exist in the pipeline but are not coach-visible
          yet. One honest footnote instead of permanently-empty cards. */}
      <Card data-testid="analytics-in-development">
        <CardContent>
          <h2 className="font-display text-xs font-semibold uppercase tracking-widest text-muted-foreground">
            In development
          </h2>
          <p className="mt-1 text-xs text-muted-foreground">
            <strong className="text-foreground/80">Model Quality</strong> — boundary / tracking /
            label / pose quality scores from the model registry, not exposed to coaches yet.{" "}
            <strong className="text-foreground/80">Spatial Heatmap</strong> — requires aggregated
            tracklet positions across many clips; arrives with the metrics-pipeline backfill.
          </p>
        </CardContent>
      </Card>
    </div>
  );
}

function scoutToCardState(
  scout: FetchState<SelfScoutResponse>,
  retry: () => void,
): AnalyticsCardState {
  switch (scout.kind) {
    case "loading":
      return { kind: "loading", label: "Computing tendencies…" };
    case "offline":
      return { kind: "unavailable", reason: OFFLINE_REASON };
    case "error":
      return { kind: "error", message: scout.message, onRetry: retry };
    case "empty":
      return {
        kind: "empty",
        reason: "No labeled plays available yet. Upload film or wait for the labeling pipeline.",
      };
    case "ready":
      return { kind: "live" };
  }
}

function FrontierCard({
  title,
  metricName,
  valueKey,
  unit,
  metrics,
  loading,
}: {
  title: string;
  metricName: string;
  valueKey: string;
  unit: string;
  metrics: FrontierMetric[] | null;
  loading: boolean;
}) {
  // Only non-suppressed metrics for this name count as a real value.
  const forName = (metrics ?? []).filter(
    (m) =>
      m.metric_name.toLowerCase() === metricName &&
      m.metric_value?.suppressed !== true,
  );
  const latest = forName[0];
  const rawValue = latest ? latest.metric_value?.[valueKey] : undefined;
  const hasValue = typeof rawValue === "number";

  const state: AnalyticsCardState = loading
    ? { kind: "loading", label: "Loading experimental metrics…" }
    : !hasValue
      ? { kind: "unavailable", reason: FRONTIER_UNAVAILABLE_REASON[metricName] }
      : { kind: "live" };

  return (
    <AnalyticsCard
      title={title}
      state={state}
      headerExtra={hasValue ? <ExperimentalBadge /> : undefined}
    >
      {hasValue && latest && (
        <div data-testid={`frontier-${metricName}`}>
          <strong data-numeric className="font-mono text-2xl font-semibold">
            {Number(rawValue).toFixed(2)}
            {unit ? ` ${unit}` : ""}
          </strong>
          <p className="mt-1 text-xs text-muted-foreground">
            {forName.length} sample{forName.length === 1 ? "" : "s"} · source {latest.source}
            {latest.sample_size != null ? ` · n=${latest.sample_size}` : ""}
          </p>
          {latest.stability_note && (
            <p className="mt-1 text-xs text-muted-foreground">{latest.stability_note}</p>
          )}
        </div>
      )}
    </AnalyticsCard>
  );
}
