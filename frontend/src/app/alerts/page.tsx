"use client";

import Link from "next/link";
import { ArrowRight, BellOff } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { FootballShell } from "@/components/shell/app-shell";
import { useAppState } from "@/lib/app-state";
import {
  actionAlert,
  fetchAlerts,
  subscribeAlerts,
  type AlertStreamHandle,
  type ApiAlert,
} from "@/lib/api";
import { ExperimentalBadge } from "@/components/experimental-badge";
import { Alert as AlertBox, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState } from "@/components/composite/empty-state";
import {
  StatusBadge,
  toneForSeverity,
  type StatusTone,
} from "@/components/composite/status-badge";

type AlertsState =
  | { kind: "loading" }
  | { kind: "offline" }
  | { kind: "error"; message: string }
  | { kind: "ready"; alerts: ApiAlert[] };

type StreamState = "idle" | "connecting" | "connected" | "degraded" | "polling";

export default function AlertsPage() {
  return (
    <FootballShell activePage="alerts">
      <AlertsView />
    </FootballShell>
  );
}

function AlertsView() {
  const { mockMode, authToken } = useAppState();
  const [state, setState] = useState<AlertsState>({ kind: "loading" });
  const [streamState, setStreamState] = useState<StreamState>("idle");
  const streamRef = useRef<AlertStreamHandle | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const loadAlerts = useCallback(async () => {
    const baseUrl = process.env.NEXT_PUBLIC_API_URL;
    if (!baseUrl) {
      setState({ kind: "offline" });
      return;
    }
    try {
      const alerts = await fetchAlerts({ limit: 50 }, authToken);
      setState({ kind: "ready", alerts });
    } catch (err) {
      setState({
        kind: "error",
        message: err instanceof Error ? err.message : String(err),
      });
    }
  }, [authToken]);

  const handleAction = useCallback(
    async (alertId: string) => {
      try {
        const updated = await actionAlert(alertId, authToken);
        setState((cur) => {
          if (cur.kind !== "ready") return cur;
          return {
            kind: "ready",
            alerts: cur.alerts.map((a) => (a.id === alertId ? updated : a)),
          };
        });
      } catch {
        // Surface nothing destructive on failure — the alert simply stays open.
      }
    },
    [authToken],
  );

  useEffect(() => {
    loadAlerts();
  }, [loadAlerts]);

  // SSE subscription with fallback to polling on error.
  useEffect(() => {
    const baseUrl = process.env.NEXT_PUBLIC_API_URL;
    if (!baseUrl || typeof fetch === "undefined") {
      setStreamState("degraded");
      return;
    }
    setStreamState("connecting");
    let cancelled = false;
    let handle: AlertStreamHandle | null = null;

    const startPolling = () => {
      if (cancelled || pollRef.current) return;
      setStreamState("polling");
      pollRef.current = setInterval(() => {
        loadAlerts();
      }, 15_000);
    };

    try {
      handle = subscribeAlerts(
        (event) => {
          if (cancelled) return;
          if (event.type === "connected") {
            setStreamState("connected");
          } else if (event.type === "alert") {
            setState((cur) => {
              if (cur.kind !== "ready") return cur;
              // De-dup by id
              if (cur.alerts.some((a) => a.id === event.alert.id)) return cur;
              return { kind: "ready", alerts: [event.alert, ...cur.alerts] };
            });
          }
        },
        () => {
          if (cancelled) return;
          setStreamState("degraded");
          handle?.close();
          handle = null;
          startPolling();
        },
        authToken,
      );
      streamRef.current = handle;
    } catch {
      startPolling();
    }

    return () => {
      cancelled = true;
      streamRef.current?.close();
      streamRef.current = null;
      handle = null;
      if (pollRef.current) {
        clearInterval(pollRef.current);
        pollRef.current = null;
      }
    };
  }, [loadAlerts, authToken]);

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHeader className="flex flex-row flex-wrap items-center justify-between gap-3">
          <div>
            <h2 className="font-display text-base font-semibold uppercase tracking-wide">
              Coaching Alerts
            </h2>
            <p className="mt-0.5 max-w-2xl text-xs text-muted-foreground">
              Live coaching alerts streamed from the analytics and biomechanics pipeline.
              Coach/analyst-only.
              {mockMode ? " Mock mode is on — only server-backed alerts appear here." : ""}
            </p>
          </div>
          <StreamBadge state={streamState} />
        </CardHeader>
      </Card>

      {state.kind === "loading" && (
        <div role="status" aria-busy="true" aria-label="Loading alerts" className="flex flex-col gap-2">
          <Skeleton className="h-16 w-full" />
          <Skeleton className="h-16 w-full" />
        </div>
      )}
      {state.kind === "offline" && (
        <EmptyState
          icon={BellOff}
          title="Alerts unavailable — not connected to the team server"
          hint="New alerts stream in automatically once the system is back online."
        />
      )}
      {state.kind === "error" && (
        <AlertBox variant="destructive" role="alert">
          <AlertTitle>Could not load alerts</AlertTitle>
          <AlertDescription>{state.message}</AlertDescription>
          <Button variant="outline" size="sm" className="mt-2 w-fit" onClick={loadAlerts}>
            Retry
          </Button>
        </AlertBox>
      )}
      {state.kind === "ready" && state.alerts.length === 0 && (
        <EmptyState
          icon={BellOff}
          title="No alerts"
          hint="No coaching alerts yet for your position group. New alerts will stream in here automatically."
        />
      )}
      {state.kind === "ready" && state.alerts.length > 0 && (
        <div className="flex flex-col gap-2">
          {state.alerts.map((a) => (
            <AlertRow key={a.id} alert={a} onAction={handleAction} />
          ))}
        </div>
      )}
    </div>
  );
}

function StreamBadge({ state }: { state: StreamState }) {
  const label: Record<StreamState, string> = {
    idle: "Idle",
    connecting: "Connecting…",
    connected: "Live (SSE)",
    degraded: "Degraded",
    polling: "Polling (15s)",
  };
  const tone: Record<StreamState, StatusTone> = {
    idle: "neutral",
    connecting: "info",
    connected: "ok",
    degraded: "danger",
    polling: "warn",
  };
  return (
    <span role="status" aria-label={`Alert stream ${label[state]}`}>
      <StatusBadge tone={tone[state]} dot>
        {label[state]}
      </StatusBadge>
    </span>
  );
}

function AlertRow({
  alert,
  onAction,
}: {
  alert: ApiAlert;
  onAction?: (alertId: string) => void;
}) {
  const isTendency = alert.alert_type === "formation_tendency";
  // Workload-risk alerts (Issue #149) only ever reach sports-performance
  // staff + admins — the backend filters them out of the list/SSE for
  // everyone else. Rendered with explicitly non-diagnostic copy.
  const isWorkloadRisk = alert.alert_type === "workload_risk";
  const mv = alert.metric_value ?? {};
  const tendencyKind = typeof mv.tendency_kind === "string" ? mv.tendency_kind : null;
  const message = typeof mv.message === "string" ? mv.message : null;
  const caveat = typeof mv.caveat === "string" ? mv.caveat : null;
  const evidence = Array.isArray(mv.evidence_clip_ids)
    ? (mv.evidence_clip_ids as unknown[]).filter((c): c is string => typeof c === "string")
    : [];
  const title = isTendency
    ? "Tendency break"
    : isWorkloadRisk
      ? "Workload review flag"
      : alert.alert_type;

  return (
    <Card data-testid={`alert-${alert.id}`}>
      <CardContent className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 flex-1 basis-64">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-[0.9rem] font-semibold">{title}</span>
            <StatusBadge tone={toneForSeverity(alert.severity)} className="capitalize">
              {alert.severity}
            </StatusBadge>
            <span className="text-xs uppercase tracking-wide text-muted-foreground">
              {alert.position_group}
            </span>
            {tendencyKind === "pattern_break" && <ExperimentalBadge label="Pattern break" />}
            {isWorkloadRisk && <ExperimentalBadge label="Sports-performance signal" />}
          </div>
          {isWorkloadRisk && (
            <p className="mt-1 text-xs text-muted-foreground">
              ACWR {typeof mv.acwr === "number" ? mv.acwr : "–"} · asymmetry{" "}
              {typeof mv.asymmetry_index === "number" ? mv.asymmetry_index : "–"}
              {caveat ? ` · ${caveat}` : ""}
            </p>
          )}
          {isTendency && message ? (
            <p className="mt-1 text-xs text-muted-foreground">{message}</p>
          ) : (
            <p className="mt-1 text-xs text-muted-foreground">
              {alert.metric_name} · confidence {Math.round(alert.confidence * 100)}%
              {alert.session_id ? ` · session ${alert.session_id}` : ""}
            </p>
          )}
          {isTendency && evidence.length > 0 && (
            <p className="mt-1 text-xs text-muted-foreground">
              Examples:{" "}
              {evidence.map((clipId, i) => (
                <span key={clipId}>
                  {i > 0 ? " · " : ""}
                  <Link
                    href={`/clip-review/?clipId=${encodeURIComponent(clipId)}`}
                    className="text-primary hover:underline"
                  >
                    clip {i + 1}
                  </Link>
                </span>
              ))}
            </p>
          )}
          <p className="mt-1 text-xs text-muted-foreground/80">
            {new Date(alert.created_at).toLocaleString()}
            {alert.is_acknowledged ? " · acknowledged" : ""}
            {alert.is_actioned ? " · actioned" : ""}
          </p>
        </div>
        <div className="flex shrink-0 flex-col items-stretch gap-1.5">
          {onAction && !alert.is_actioned && (
            <Button
              variant="outline"
              size="sm"
              data-testid={`action-${alert.id}`}
              onClick={() => onAction(alert.id)}
            >
              Mark actioned
            </Button>
          )}
          {alert.clip_id && (
            <Button asChild variant="secondary" size="sm">
              <Link href={`/clip-review/?clipId=${encodeURIComponent(alert.clip_id)}`}>
                Open clip <ArrowRight className="size-3.5" />
              </Link>
            </Button>
          )}
        </div>
      </CardContent>
    </Card>
  );
}
