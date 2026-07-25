"use client";

/**
 * Athlete health/workload surface (Issue #113). Role-gated: only
 * sports-performance staff (plus analytics leads / admins) see the surface; a
 * deep link from any other role falls through to a restricted notice. All
 * values come from the backend (dashboard + injury-risk rollups) or are
 * clearly-labeled integration contracts; the old illustrative "Team Load
 * Trend" mock chart was removed (#96).
 */

import { ShieldAlert } from "lucide-react";
import { useEffect, useState } from "react";
import { useAppState } from "@/lib/app-state";
import { fetchHealthDashboard, fetchInjuryRisk } from "@/lib/api";
import { ExperimentalBadge } from "@/components/experimental-badge";
import { TrendLine } from "@/components/shared/trend-line";
import { canAccessHealthWorkload, canSeePlayerLevelRisk } from "@/lib/roles";
import {
  HEALTH_POLICY_STATEMENT,
  HEALTH_WORKLOAD_DISCLAIMER,
  HEALTH_WORKLOAD_INTEGRATIONS,
  WORKLOAD_RISK_CAVEAT,
  type HealthDashboardResponse,
  type HealthWorkloadIntegration,
  type InjuryRiskResponse,
} from "@/lib/health-workload";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { StatusBadge, type StatusTone } from "@/components/composite/status-badge";
import { EmptyState } from "@/components/composite/empty-state";

function SectionTitle({ children }: { children: React.ReactNode }) {
  return (
    <h2 className="flex flex-wrap items-center gap-2 font-display text-base font-semibold uppercase tracking-wide">
      {children}
    </h2>
  );
}

export function HealthWorkloadView() {
  const { currentRole } = useAppState();

  if (!canAccessHealthWorkload(currentRole)) {
    return (
      <div data-testid="health-workload-restricted">
        <EmptyState
          icon={ShieldAlert}
          title="Health & Workload — Restricted"
          hint="This surface is limited to sports-performance staff (plus analytics leads and admins). Your role does not have access. Contact an administrator if you believe this is in error."
        />
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-4" data-testid="health-workload-surface">
      <Card data-testid="health-workload-disclaimer">
        <CardHeader>
          <SectionTitle>Sports-Performance Context</SectionTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-1.5 text-xs text-muted-foreground">
          <p>{HEALTH_WORKLOAD_DISCLAIMER}</p>
          <p data-testid="health-policy-statement">{HEALTH_POLICY_STATEMENT}</p>
        </CardContent>
      </Card>

      <DailyAthleteStatePanel />

      <WorkloadRiskPanel />

      <Card>
        <CardHeader>
          <SectionTitle>Data Sources</SectionTitle>
          <p className="mt-0.5 text-xs text-muted-foreground">
            No source is connected yet — these are documented integration contracts only. No
            athlete data is surfaced.
          </p>
        </CardHeader>
        <CardContent className="flex flex-col gap-2">
          {HEALTH_WORKLOAD_INTEGRATIONS.map((integration) => (
            <IntegrationRow key={integration.source} integration={integration} />
          ))}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <SectionTitle>About This Surface</SectionTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-2">
          <Insight
            title="Access"
            detail="Sports-performance staff, analytics leads, and admins only. Every read is audit-logged."
            severity="info"
          />
          <Insight
            title="Purpose"
            detail="Training-load and wellness context to support staff planning and conversations."
            severity="good"
          />
          <Insight
            title="Not for"
            detail="Medical diagnosis, injury prediction, or return-to-play decisions. This is not a medical device."
            severity="warning"
          />
        </CardContent>
      </Card>
    </div>
  );
}

// Unified daily athlete-state dashboard (Issue #9). Fuses the nightly CV
// workload rollup with the UT sources (wellness, GPS, S&C, academic calendar,
// rehab-tier injury history). The backend shapes the payload per role — this
// component only renders what it receives: player-level state + fatigue flags
// for sports-performance staff/admins, position-group aggregates for
// analysts. Every value carries its source and confidence caveat.
function DailyAthleteStatePanel() {
  const { authToken } = useAppState();
  const [dashboard, setDashboard] = useState<HealthDashboardResponse | null>(null);
  const [dashState, setDashState] = useState<"idle" | "loading" | "ready" | "error">("idle");

  useEffect(() => {
    if (!authToken) {
      setDashboard(null);
      setDashState("idle");
      return;
    }
    let cancelled = false;
    setDashState("loading");
    fetchHealthDashboard({ days: 14 }, authToken)
      .then((payload) => {
        if (cancelled) return;
        setDashboard(payload);
        setDashState("ready");
      })
      .catch(() => {
        if (cancelled) return;
        setDashboard(null);
        setDashState("error");
      });
    return () => {
      cancelled = true;
    };
  }, [authToken]);

  const players = dashboard?.players ?? [];
  const aggregates = dashboard?.aggregates ?? [];
  const fatigueFlags = dashboard?.fatigue_flags ?? [];
  const trends = dashboard?.trends ?? {};
  const academic = dashboard?.academic_context ?? [];
  const connectedSources =
    dashboard?.integrations?.filter((i) => i.status === "connected") ?? [];

  return (
    <Card data-testid="health-daily-state">
      <CardHeader>
        <SectionTitle>
          Daily Athlete State <ExperimentalBadge label="Staff context" />
        </SectionTitle>
        <p className="mt-0.5 text-xs text-muted-foreground">
          Fused view: CV workload rollup joined with wellness, GPS/wearables, S&amp;C, academic
          calendar, and availability context. Values are shown with their source and confidence —
          nothing here is ground truth.
        </p>
      </CardHeader>
      <CardContent>
        {dashState === "idle" && (
          <p className="text-xs text-muted-foreground">
            Sign in to load the fused daily athlete state.
          </p>
        )}
        {dashState === "loading" && (
          <p className="text-xs text-muted-foreground">Loading daily athlete state…</p>
        )}
        {dashState === "error" && (
          <p className="text-xs text-muted-foreground">
            Could not load the dashboard. Nothing is shown rather than stale values.
          </p>
        )}

        {dashState === "ready" && dashboard && (
          <>
            <p className="text-xs text-muted-foreground">
              {dashboard.date} · sources connected:{" "}
              {connectedSources.length > 0
                ? connectedSources.map((s) => s.display_name).join(", ")
                : "none yet (CV rollup only)"}
              {academic.length > 0 &&
                ` · academic context: ${academic
                  .map((e) => String(e.label ?? e.event_type))
                  .join(", ")}`}
            </p>

            {fatigueFlags.length > 0 && (
              <div className="mt-3 flex flex-col gap-2" data-testid="fatigue-flags">
                {fatigueFlags.map((flag) => (
                  <Insight
                    key={flag.alert_id}
                    title={`Fatigue flag — ${flag.position_group} (${flag.severity})`}
                    detail={`Unacknowledged workload-risk alert from the nightly rollup. ${flag.caveat}`}
                    severity="warning"
                  />
                ))}
              </div>
            )}

            {dashboard.player_level && players.length > 0 && (
              <div className="mt-3 overflow-x-auto">
                <Table data-testid="daily-state-table">
                  <TableHeader>
                    <TableRow>
                      <TableHead>Player</TableHead>
                      <TableHead>Group</TableHead>
                      <TableHead>CV load (yd)</TableHead>
                      <TableHead>ACWR</TableHead>
                      <TableHead>Wellness</TableHead>
                      <TableHead>GPS load</TableHead>
                      <TableHead>S&amp;C RPE</TableHead>
                      <TableHead>Availability</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {players.map((state) => (
                      <TableRow key={state.player_id}>
                        <TableCell className="font-medium">
                          {state.name}
                          {state.jersey_number != null ? ` #${state.jersey_number}` : ""}
                        </TableCell>
                        <TableCell>{state.position_group ?? "—"}</TableCell>
                        <TableCell data-numeric className="font-mono text-xs">
                          {state.cv_workload?.daily_load ?? "—"}
                        </TableCell>
                        <TableCell data-numeric className="font-mono text-xs">
                          {state.cv_workload?.acwr ?? "—"}
                        </TableCell>
                        <TableCell className="text-xs">
                          {state.wellness
                            ? `soreness ${state.wellness.soreness ?? "–"} · sleep ${state.wellness.sleep_hours ?? "–"}h`
                            : "—"}
                        </TableCell>
                        <TableCell data-numeric className="font-mono text-xs">
                          {(state.gps?.player_load as number | undefined) ?? "—"}
                        </TableCell>
                        <TableCell data-numeric className="font-mono text-xs">
                          {(state.strength_conditioning?.session_rpe as number | undefined) ?? "—"}
                        </TableCell>
                        <TableCell className="text-xs">
                          {state.injury_history == null
                            ? "restricted"
                            : state.injury_history.length === 0
                              ? "available"
                              : `${state.injury_history.length} active`}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
            )}
            {dashboard.player_level && players.length === 0 && (
              <p className="mt-3 text-xs text-muted-foreground">
                No fused rows for this day yet — rows appear after the nightly workload rollup has
                run.
              </p>
            )}

            {!dashboard.player_level && (
              <div className="mt-3 flex flex-col gap-2" data-testid="daily-state-aggregates">
                {aggregates.length === 0 ? (
                  <Insight
                    title="Position-group view"
                    detail="No aggregate rows for this day yet. Player-level state is limited to sports-performance staff."
                    severity="info"
                  />
                ) : (
                  aggregates.map((agg) => (
                    <Insight
                      key={agg.position_group}
                      title={`${agg.position_group} — ${agg.player_count} player${agg.player_count === 1 ? "" : "s"}`}
                      detail={`Mean ACWR ${agg.mean_acwr ?? "–"} · mean load ${agg.mean_daily_load ?? "–"} yd. ${agg.caveat}`}
                      severity="info"
                    />
                  ))
                )}
              </div>
            )}

            {Object.keys(trends).length > 0 && (
              <div className="mt-4" data-testid="workload-trends">
                <h3 className="font-display text-sm font-semibold uppercase tracking-wide">
                  Workload trends by position group (mean ACWR, {dashboard.days}d)
                </h3>
                <div className="mt-2 flex flex-col gap-2">
                  {Object.entries(trends).map(([group, points]) => {
                    const series = points
                      .map((p) => p.mean_acwr)
                      .filter((v): v is number => v != null);
                    return (
                      <div key={group} className="grid grid-cols-[80px_1fr] items-center gap-3">
                        <span className="font-display text-sm font-semibold uppercase">
                          {group}
                        </span>
                        {series.length >= 2 ? (
                          <TrendLine data={series} />
                        ) : (
                          <span className="text-xs text-muted-foreground">—</span>
                        )}
                      </div>
                    );
                  })}
                </div>
              </div>
            )}
          </>
        )}
      </CardContent>
    </Card>
  );
}

// Workload-risk signals panel (Issue #149). Player-level rows (ACWR, gait
// asymmetry, heuristic risk score, sprint count) render only for
// sports-performance staff + admins; analysts see position-group aggregates.
// The backend enforces the same split — this gate is presentation only.
// Every value ships with the non-diagnostic caveat.
function WorkloadRiskPanel() {
  const { currentRole, authToken } = useAppState();
  const [risk, setRisk] = useState<InjuryRiskResponse | null>(null);
  const [riskState, setRiskState] = useState<"idle" | "loading" | "ready" | "error">("idle");

  useEffect(() => {
    if (!authToken) {
      setRisk(null);
      setRiskState("idle");
      return;
    }
    let cancelled = false;
    setRiskState("loading");
    fetchInjuryRisk({ days: 14 }, authToken)
      .then((payload) => {
        if (cancelled) return;
        setRisk(payload);
        setRiskState("ready");
      })
      .catch(() => {
        if (cancelled) return;
        setRisk(null);
        setRiskState("error");
      });
    return () => {
      cancelled = true;
    };
  }, [authToken]);

  const playerLevel = canSeePlayerLevelRisk(currentRole);
  const rows = risk?.rows ?? [];
  const aggregates = risk?.aggregates ?? [];
  const flagged = rows.filter((row) => {
    const latest = row.latest;
    if (!latest) return false;
    return (latest.acwr ?? 0) > 1.5 || (latest.asymmetry_index ?? 0) > 1.3;
  });

  return (
    <Card data-testid="health-workload-risk">
      <CardHeader>
        <SectionTitle>
          Workload Risk Signals <ExperimentalBadge />
        </SectionTitle>
        <p className="mt-0.5 text-xs text-muted-foreground" data-testid="workload-risk-caveat">
          {WORKLOAD_RISK_CAVEAT} Values come from the nightly CV workload rollup (acute:chronic
          ratio over 7/28 days, pose-based gait asymmetry).
        </p>
      </CardHeader>
      <CardContent>
        {riskState === "idle" && (
          <p className="text-xs text-muted-foreground">
            Sign in to load the nightly workload rollup.
          </p>
        )}
        {riskState === "loading" && (
          <p className="text-xs text-muted-foreground">Loading nightly rollup…</p>
        )}
        {riskState === "error" && (
          <p className="text-xs text-muted-foreground">
            Could not load workload risk data. The surface stays empty rather than showing stale or
            illustrative risk values.
          </p>
        )}

        {riskState === "ready" && playerLevel && (
          <>
            {flagged.length > 0 && (
              <div className="mb-3 flex flex-col gap-2" data-testid="workload-risk-flags">
                {flagged.map((row) => {
                  const latest = row.latest;
                  const reasons = latest?.risk_reason_codes?.join(", ") ?? "";
                  return (
                    <Insight
                      key={row.player_id}
                      title={`${row.name} — review flagged`}
                      detail={`ACWR ${latest?.acwr ?? "–"} · asymmetry ${latest?.asymmetry_index ?? "–"} · ${reasons}. ${WORKLOAD_RISK_CAVEAT}`}
                      severity="warning"
                    />
                  );
                })}
              </div>
            )}
            {rows.length === 0 ? (
              <p className="text-xs text-muted-foreground">
                No rollup rows yet — rows appear after the nightly workload job has processed
                identity-confident film.
              </p>
            ) : (
              <div className="overflow-x-auto">
                <Table data-testid="workload-risk-table">
                  <TableHeader>
                    <TableRow>
                      <TableHead>Player</TableHead>
                      <TableHead>Group</TableHead>
                      <TableHead>ACWR (7d/28d)</TableHead>
                      <TableHead>Asymmetry</TableHead>
                      <TableHead>Risk score</TableHead>
                      <TableHead>Sprints</TableHead>
                      <TableHead>Confidence</TableHead>
                      <TableHead className="min-w-30">14-day ACWR</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {rows.map((row) => {
                      const latest = row.latest;
                      const acwrSeries = row.series
                        .map((point) => point.acwr)
                        .filter((value): value is number => value != null);
                      return (
                        <TableRow key={row.player_id}>
                          <TableCell className="font-medium">
                            {row.name}
                            {row.jersey_number != null ? ` #${row.jersey_number}` : ""}
                          </TableCell>
                          <TableCell>{row.position_group ?? "—"}</TableCell>
                          <TableCell data-numeric className="font-mono text-xs">
                            {latest?.acwr ?? "—"}
                          </TableCell>
                          <TableCell data-numeric className="font-mono text-xs">
                            {latest?.asymmetry_index ?? "—"}
                          </TableCell>
                          <TableCell data-numeric className="font-mono text-xs">
                            {latest?.injury_risk_score ?? "—"}
                          </TableCell>
                          <TableCell data-numeric className="font-mono text-xs">
                            {latest?.sprint_count ?? "—"}
                          </TableCell>
                          <TableCell data-numeric className="font-mono text-xs">
                            {latest?.confidence != null ? latest.confidence.toFixed(2) : "—"}
                          </TableCell>
                          <TableCell className="min-w-30">
                            {acwrSeries.length >= 2 ? <TrendLine data={acwrSeries} /> : "—"}
                          </TableCell>
                        </TableRow>
                      );
                    })}
                  </TableBody>
                </Table>
              </div>
            )}
          </>
        )}

        {riskState === "ready" && !playerLevel && (
          <div className="flex flex-col gap-2" data-testid="workload-risk-aggregates">
            <Insight
              title="Position-group view"
              detail="Your role sees aggregate workload context only. Player-level risk is limited to sports-performance staff."
              severity="info"
            />
            {aggregates.map((agg) => (
              <Insight
                key={agg.position_group}
                title={`${agg.position_group} — ${agg.player_count} player${agg.player_count === 1 ? "" : "s"}`}
                detail={`Mean ACWR ${agg.mean_acwr ?? "–"} · mean asymmetry ${agg.mean_asymmetry_index ?? "–"} · max risk ${agg.max_injury_risk_score ?? "–"}. ${WORKLOAD_RISK_CAVEAT}`}
                severity="info"
              />
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function IntegrationRow({ integration }: { integration: HealthWorkloadIntegration }) {
  const connected = integration.status === "connected";
  return (
    <div
      className="flex items-center justify-between gap-3 border-b border-border-soft pb-2 last:border-b-0 last:pb-0"
      data-testid={`hw-integration-${integration.source}`}
    >
      <div className="min-w-0">
        <span className="text-[0.85rem] font-semibold">{integration.displayName}</span>
        <div className="mt-0.5 text-xs text-muted-foreground">
          {integration.description}
          {" · "}
          {integration.dataCategories.join(", ")}
        </div>
      </div>
      <StatusBadge tone={connected ? "ok" : "warn"} dot className="whitespace-nowrap">
        {connected ? "Connected" : "Not connected"}
      </StatusBadge>
    </div>
  );
}

const INSIGHT_TONE: Record<"good" | "warning" | "danger" | "info", StatusTone> = {
  good: "ok",
  warning: "warn",
  danger: "danger",
  info: "info",
};

function Insight({
  title,
  detail,
  severity,
}: {
  title: string;
  detail: string;
  severity: "good" | "warning" | "danger" | "info";
}) {
  return (
    <div className="grid grid-cols-[auto_1fr] items-start gap-2.5">
      <StatusBadge tone={INSIGHT_TONE[severity]} className="size-6 justify-center p-0" aria-hidden>
        •
      </StatusBadge>
      <span className="min-w-0">
        <strong className="text-[0.85rem]">{title}</strong>
        <br />
        <small className="text-xs text-muted-foreground">{detail}</small>
      </span>
    </div>
  );
}
