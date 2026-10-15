"use client";

import { useEffect, useState } from "react";
import type { PageKey } from "@/lib/types";
import { useOptionalAuth } from "@/lib/auth";
import { Sheet, SheetContent, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { AppTopbar } from "@/components/shell/app-topbar";
import { SidebarContent } from "@/components/shell/app-sidebar";
import { PageHeader } from "@/components/composite/page-header";

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
  const openingLibrary = Boolean(auth && (!auth.ready || (auth.authRequired && !auth.token)));
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

  return (
    <div className="min-h-screen bg-background">
      <AppTopbar activePage={activePage} onMenuClick={() => setMobileNavOpen(true)} />

      <Sheet open={mobileNavOpen} onOpenChange={setMobileNavOpen}>
        <SheetContent side="left" className="w-72 gap-0 p-0">
          <SheetHeader className="border-b border-border-soft px-4 py-3">
            <SheetTitle className="text-base font-semibold">
              Football IQ
            </SheetTitle>
          </SheetHeader>
          <SidebarContent activeKey={activePage} onNavigate={() => setMobileNavOpen(false)} />
        </SheetContent>
      </Sheet>

      <div className="lg:grid lg:grid-cols-[216px_minmax(0,1fr)]">
        <aside className="sticky top-16 hidden h-[calc(100vh-4rem)] overflow-y-auto border-r border-border-soft bg-[oklch(0.17_0.026_252)] lg:block">
          <SidebarContent activeKey={activePage} />
        </aside>

        <main id="main-content" className="min-w-0 px-4 py-6 lg:px-7 lg:py-7">
          <div className="mx-auto w-full max-w-[1520px]">
            <PageHeader page={activePage} actions={headerActions} />
            {openingLibrary ? <div role="status" className="rounded-xl border border-border-soft bg-card p-6 text-sm text-muted-foreground">{auth?.ready ? "Opening sign-in…" : "Opening your library…"}</div> : children}
          </div>
        </main>
      </div>
    </div>
  );
}
