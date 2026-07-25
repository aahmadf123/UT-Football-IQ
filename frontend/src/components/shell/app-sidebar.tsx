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
  UserRound,
  UsersRound,
} from "lucide-react";
import type { PageKey } from "@/lib/types";
import { useAppState, type ApiStatus } from "@/lib/app-state";
import { canAccessHealthWorkload } from "@/lib/roles";
import { StatusBadge, type StatusTone } from "@/components/composite/status-badge";
import { cn } from "@/lib/utils";

// Consolidated coach-facing navigation (ADR 0003). Film Room and Scouting are
// workspace hubs; Analytics is surfaced as "Model Insights". Alerts live in
// the top-bar inbox bell, and College Data lives inside Scouting.
const NAV_ITEMS = [
  { key: "dashboard", label: "Dashboard", href: "/", icon: Home },
  { key: "film-room", label: "Film Room", href: "/film-room", icon: Clapperboard },
  { key: "scouting", label: "Scouting", href: "/scouting", icon: ClipboardList },
  { key: "players", label: "Players", href: "/players", icon: UserRound },
  { key: "player-development", label: "Player Development", href: "/player-development", icon: UsersRound },
  { key: "health-workload", label: "Health & Workload", href: "/health-workload", icon: HeartPulse },
  { key: "analytics", label: "Model Insights", href: "/analytics", icon: LineChart },
  { key: "reports", label: "Reports", href: "/reports", icon: FileText },
  { key: "settings", label: "Settings", href: "/settings", icon: Settings },
] as const;

function apiStatusLabel(status: ApiStatus): string {
  switch (status) {
    case "live":
      return "Connected";
    case "loading":
      return "Connecting…";
    case "offline":
      return "Offline";
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
  const { apiStatus, uploads, currentRole } = useAppState();

  // Health & Workload is hidden unless the role is approved (Issue #113). The
  // page itself also gates its content, so a deep link can't bypass this.
  const visibleItems = NAV_ITEMS.filter(
    (item) => item.key !== "health-workload" || canAccessHealthWorkload(currentRole),
  );

  return (
    <div className="flex h-full flex-col">
      <nav aria-label="Football IQ navigation" className="flex flex-col gap-1 p-3">
        {visibleItems.map((item) => {
          const Icon = item.icon;
          const isActive = activeKey === item.key;
          return (
            <Link
              key={item.key}
              href={item.href}
              onClick={onNavigate}
              aria-current={isActive ? "page" : undefined}
              className={cn(
                "relative flex min-h-11 items-center gap-3 rounded-md px-3 font-display text-[0.86rem] font-semibold uppercase tracking-wider transition-colors",
                isActive
                  ? "bg-gradient-to-b from-primary to-gold-strong text-primary-foreground"
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

      <div className="mt-auto flex flex-col gap-2 border-t border-border-soft p-3">
        <div className="flex items-center justify-between gap-2" data-testid="system-status">
          <StatusBadge tone={apiStatusTone(apiStatus)} dot>
            {apiStatusLabel(apiStatus)}
          </StatusBadge>
          {uploads.length > 0 ? (
            <span data-numeric className="font-mono text-xs text-muted-foreground">
              {uploads.length} queued
            </span>
          ) : null}
        </div>
        <p className="font-display text-[0.78rem] font-semibold uppercase tracking-[0.18em] text-primary">
          #TeamToledo · Rise Together
        </p>
      </div>
    </div>
  );
}
