"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { BellRing } from "lucide-react";
import { fetchAlerts } from "@/lib/api";
import { useOptionalAuth } from "@/lib/auth";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

/**
 * Topbar entry point to the alerts inbox — the one home for coaching alerts.
 * Shows a real unread (unacknowledged) count; quiet when offline or in mock
 * mode so it never fabricates urgency.
 */
export function AlertsBell({ active = false }: { active?: boolean }) {
  const auth = useOptionalAuth();
  const token = auth?.token;
  const [unread, setUnread] = useState(0);

  useEffect(() => {
    if (!process.env.NEXT_PUBLIC_API_URL) return;
    let cancelled = false;

    const load = async () => {
      try {
        const alerts = await fetchAlerts({ acknowledged: false, limit: 10 }, token);
        if (!cancelled) setUnread(alerts.length);
      } catch {
        // Quiet failure: the bell simply shows no count.
      }
    };

    void load();
    const id = setInterval(load, 60_000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [token]);

  const countLabel = unread > 9 ? "9+" : String(unread);

  return (
    <Button
      asChild
      variant={active ? "secondary" : "ghost"}
      size="icon"
      className="relative size-10"
    >
      <Link
        href="/alerts"
        aria-label={unread > 0 ? `Coaching alerts, ${countLabel} unread` : "Coaching alerts inbox"}
        title="Coaching alerts inbox"
      >
        <BellRing className="size-[18px]" />
        {unread > 0 ? (
          <span
            data-testid="alerts-unread-count"
            className={cn(
              "absolute -right-0.5 -top-0.5 flex h-4 min-w-4 items-center justify-center rounded-full",
              "bg-status-danger px-1 font-mono text-[0.62rem] font-semibold leading-none text-white",
            )}
          >
            {countLabel}
          </span>
        ) : null}
      </Link>
    </Button>
  );
}
