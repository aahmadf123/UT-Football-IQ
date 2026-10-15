"use client";

import Link from "next/link";
import {
  ClipboardList,
  Clapperboard,
  FileText,
  HeartPulse,
  Home,
  LineChart,
  Settings,
  ChevronDown,
  UserRound,
  ArrowUpRight,
  Database,
} from "lucide-react";
import type { PageKey } from "@/lib/types";
import { useAppState, type ApiStatus } from "@/lib/app-state";
import { canAccessHealthWorkload } from "@/lib/roles";
import { StatusBadge, type StatusTone } from "@/components/composite/status-badge";
import { cn } from "@/lib/utils";
import { useWorkspaceContext } from "@/lib/workspace-context";

// Consolidated coach-facing navigation (ADR 0003). Film Room and Scouting are
// workspace hubs; Analytics is surfaced as "Model Insights". Player
// Development lives in Players → Development; College Data lives inside
// Scouting. Alerts get both a sidebar entry and the top-bar inbox bell.
const NAV_ITEMS = [
  { key: "dashboard", label: "Today", href: "/", icon: Home },
  { key: "film-room", label: "Film Room", href: "/film-room", icon: Clapperboard },
  { key: "scouting", label: "Scouting", href: "/scouting", icon: ClipboardList },
  { key: "players", label: "Players", href: "/players", icon: UserRound },
  { key: "reports", label: "Reports", href: "/reports", icon: FileText },
] as const;

function apiStatusLabel(status: ApiStatus): string {
  switch (status) {
    case "live":
      return "Connected";
    case "loading":
      return "Connecting…";
    case "offline":
      return "Offline";
    case "error":
      return "Needs attention";
    case "mock":
      return "Mock data";
    case "idle":
    default:
      return "Standby";
  }
}

function apiStatusTone(status: ApiStatus): StatusTone {
  switch (status) {
    case "live":
      return "ok";
    case "loading":
      return "info";
    case "mock":
      return "warn";
    case "offline":
      return "warn";
    case "error":
      return "danger";
    case "idle":
    default:
      return "neutral";
  }
}

/**
 * Sidebar content: nav plus a one-line system footer. Shared between the
 * desktop rail and the mobile Sheet drawer. Replaces the old Session Summary,
 * duplicate brand panel, and the entire right rail (System Status condensed
 * to one line; Confidence Score deleted — it could never show data; Model
 * Pipeline's home is Film Room → Upload & Processing).
 */
export function SidebarContent({
  activeKey,
  onNavigate,
}: {
  activeKey: PageKey;
  onNavigate?: () => void;
}) {
  const { apiStatus, currentRole } = useAppState();
  const workspace = useWorkspaceContext();

  // Health & Workload is hidden unless the role is approved (Issue #113). The
  // page itself also gates its content, so a deep link can't bypass this.
  const tools = [
    { key: "analytics", label: "Model insights", href: "/analytics/", icon: LineChart },
    ...(canAccessHealthWorkload(currentRole)
      ? [{ key: "health-workload", label: "Health & workload", href: "/health-workload/", icon: HeartPulse }]
      : []),
  ];

  return (
    <div className="flex h-full flex-col">
      <Link href="/film-room/" onClick={onNavigate} className="workspace-surface m-3 mb-5 flex items-center gap-3 rounded-lg border border-border-soft p-3 hover:border-primary/35">
        <span className="flex size-9 shrink-0 items-center justify-center rounded-md border border-primary/25 bg-primary/10 text-xs font-bold tracking-wide text-primary">UT</span>
        <span className="min-w-0 flex-1"><span className="block text-xs font-semibold text-foreground">Toledo Rockets</span><span className="mt-1 block text-[11px] text-muted-foreground">{workspace.libraryLabel}</span></span>
        <ArrowUpRight className="size-3.5 text-muted-foreground" aria-hidden />
      </Link>
      <p className="section-eyebrow px-6 pb-2">Workspace</p>
      <nav aria-label="Football IQ navigation" className="flex flex-col gap-1 px-3">
        {NAV_ITEMS.map((item) => {
          const Icon = item.icon;
          const isActive = activeKey === item.key;
          return (
            <Link
              key={item.key}
              href={item.href}
              onClick={onNavigate}
              aria-current={isActive ? "page" : undefined}
              className={cn(
                "relative flex min-h-11 items-center gap-3 rounded-md px-3 text-[13px] font-medium transition-colors",
                isActive
                  ? "bg-primary/10 text-primary"
                  : "text-muted-foreground hover:bg-accent hover:text-foreground",
              )}
            >
              {isActive ? (
                <span
                  aria-hidden
                  className="absolute inset-y-3 left-0 w-[2px] rounded-full bg-primary"
                />
              ) : null}
              <Icon aria-hidden className="size-[18px] shrink-0" />
              <span className="truncate">{item.label}</span>
            </Link>
          );
        })}
      </nav>

      <details className="mx-3 mt-4 rounded-lg" open={activeKey === "analytics" || activeKey === "health-workload" || undefined}>
        <summary className="flex cursor-pointer list-none items-center justify-between px-3 py-2 text-xs font-medium text-muted-foreground">
          Advanced tools <ChevronDown className="size-4" aria-hidden />
        </summary>
        <nav aria-label="Advanced tools" className="flex flex-col gap-1">
          {tools.map(({ key, label, href, icon: Icon }) => (
            <Link key={key} href={href} onClick={onNavigate} aria-current={activeKey === key ? "page" : undefined}
              className={cn("flex min-h-10 items-center gap-3 rounded-lg px-3 text-sm", activeKey === key ? "bg-accent text-foreground" : "text-muted-foreground hover:bg-accent")}>
              <Icon className="size-4" aria-hidden />{label}
            </Link>
          ))}
        </nav>
      </details>

      <div className="mt-auto flex flex-col gap-2 border-t border-border-soft p-3">
        <div className="flex items-center justify-between gap-2 px-2" data-testid="system-status">
          <StatusBadge tone={apiStatusTone(apiStatus)} dot>
            {apiStatusLabel(apiStatus)}
          </StatusBadge>
          <Database className="size-3.5 text-muted-foreground" aria-hidden />
        </div>
        <Link href="/settings/" onClick={onNavigate} className="flex min-h-10 items-center gap-3 rounded-lg px-2 text-sm text-muted-foreground hover:bg-accent hover:text-foreground" aria-current={activeKey === "settings" ? "page" : undefined}>
          <Settings className="size-4" aria-hidden /> {workspace.settingsLabel}
        </Link>
        <p className="truncate px-2 text-[10px] text-muted-foreground" title={workspace.host}>{workspace.host}</p>
      </div>
    </div>
  );
}
