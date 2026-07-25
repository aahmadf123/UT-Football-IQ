"use client";

import { useEffect, useState } from "react";
import type { PageKey } from "@/lib/types";
import { useOptionalAuth } from "@/lib/auth";
import { Sheet, SheetContent, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { AppTopbar } from "@/components/shell/app-topbar";
import { SidebarContent } from "@/components/shell/app-sidebar";
import { PageHeader } from "@/components/composite/page-header";

// Compatibility routes (ADR 0003) that fold into a hub. Used to keep the hub
// nav item highlighted when a coach lands on an old deep link.
const COMPAT_ACTIVE: Partial<Record<PageKey, PageKey>> = {
  library: "film-room",
  "video-and-plays": "film-room",
  "clips-highlights": "film-room",
  "self-scout": "scouting",
  "opponent-scout": "scouting",
  "college-data": "scouting",
};

export function FootballShell({
  activePage,
  headerActions,
  children,
}: {
  activePage: PageKey;
  headerActions?: React.ReactNode;
  children: React.ReactNode;
}) {
  const auth = useOptionalAuth();
  const [mobileNavOpen, setMobileNavOpen] = useState(false);

  // Backend-configured deployments require a session; mock/offline demo mode
  // (no NEXT_PUBLIC_API_URL) stays browsable without one. A full navigation
  // (not router.replace) keeps the auth boundary dead simple for the static
  // export and for tests that render the shell without providers.
  const mustSignIn = Boolean(auth && auth.ready && auth.authRequired && !auth.token);
  useEffect(() => {
    if (mustSignIn && typeof window !== "undefined") {
      // Never redirect from the login page itself: if a static host
      // mis-serves /login/ with this shell, redirecting again would reload
      // the same document forever, hammering the API on every iteration.
      const path = window.location.pathname;
      if (path === "/login" || path.startsWith("/login/")) return;
      window.location.replace("/login/");
    }
  }, [mustSignIn]);

  const navActiveKey = COMPAT_ACTIVE[activePage] ?? activePage;

  return (
    <div className="min-h-screen">
      <AppTopbar activePage={activePage} onMenuClick={() => setMobileNavOpen(true)} />

      <Sheet open={mobileNavOpen} onOpenChange={setMobileNavOpen}>
        <SheetContent side="left" className="w-72 gap-0 p-0">
          <SheetHeader className="border-b border-border-soft px-4 py-3">
            <SheetTitle className="font-display text-base font-semibold uppercase tracking-wider">
              Football IQ
            </SheetTitle>
          </SheetHeader>
          <SidebarContent activeKey={navActiveKey} onNavigate={() => setMobileNavOpen(false)} />
        </SheetContent>
      </Sheet>

      <div className="lg:grid lg:grid-cols-[248px_minmax(0,1fr)]">
        <aside className="sticky top-14 hidden h-[calc(100vh-3.5rem)] overflow-y-auto border-r border-border-soft lg:block">
          <SidebarContent activeKey={navActiveKey} />
        </aside>

        <main className="min-w-0 px-4 py-5 lg:px-6">
          <div className="mx-auto w-full max-w-[1400px]">
            <PageHeader page={activePage} actions={headerActions} />
            {children}
          </div>
        </main>
      </div>
    </div>
  );
}
