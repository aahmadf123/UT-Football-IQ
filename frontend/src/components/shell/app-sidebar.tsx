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
} from "lucide-react";
import type { PageKey } from "@/lib/types";
import { useAppState, type ApiStatus } from "@/lib/app-state";
import { canAccessHealthWorkload } from "@/lib/roles";
import { StatusBadge, type StatusTone } from "@/components/composite/status-badge";
import { cn } from "@/lib/utils";

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
      <nav aria-label="Football IQ navigation" className="flex flex-col gap-1 p-3">
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
                "relative flex min-h-12 items-center gap-3 rounded-lg px-3 text-sm font-medium transition-colors",
                isActive
                  ? "bg-primary/12 text-primary"
                  : "text-muted-foreground hover:bg-accent hover:text-foreground",
              )}
            >
              {isActive ? (
                <span
                  aria-hidden
                  className="absolute inset-y-0 left-0 w-[3px] rounded-l-md bg-primary-foreground/45"
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
        <div className="flex items-center justify-between gap-2" data-testid="system-status">
          <StatusBadge tone={apiStatusTone(apiStatus)} dot>
            {apiStatusLabel(apiStatus)}
          </StatusBadge>
        </div>
        <Link href="/settings/" onClick={onNavigate} className="flex min-h-10 items-center gap-3 rounded-lg px-2 text-sm text-muted-foreground hover:bg-accent hover:text-foreground" aria-current={activeKey === "settings" ? "page" : undefined}>
          <Settings className="size-4" aria-hidden /> This computer
        </Link>
        <p className="px-2 text-xs text-muted-foreground">Toledo Football · Local library</p>
      </div>
    </div>
  );
}
