"use client";

/**
 * Canonical player profile route for the static export: /players/detail/?id=…
 *
 * A query-param CSR page works for ANY real player id from a cold static
 * load — unlike the old /players/[id] dynamic segment, whose
 * generateStaticParams could only enumerate ids known at build time.
 */

import Link from "next/link";
import { UserRound } from "lucide-react";
import { useSearchParams } from "next/navigation";
import { Suspense } from "react";
import { FootballShell } from "@/components/shell/app-shell";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/composite/empty-state";
import { PlayerProfileClient } from "./player-profile-client";

export default function PlayerDetailPage() {
  return (
    <Suspense
      fallback={
        <div role="status" className="p-6">
          <p className="text-xs text-muted-foreground">Loading player profile…</p>
        </div>
      }
    >
      <PlayerDetailContent />
    </Suspense>
  );
}

function PlayerDetailContent() {
  const searchParams = useSearchParams();
  const id = searchParams.get("id") ?? "";
  if (!id) {
    return (
      <FootballShell activePage="players">
        <EmptyState
          icon={UserRound}
          title="No player selected"
          hint="Open a player from the roster."
          action={
            <Button asChild variant="outline">
              <Link href="/players">Go to roster</Link>
            </Button>
          }
        />
      </FootballShell>
    );
  }
  return <PlayerProfileClient id={id} />;
}
