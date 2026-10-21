import { FootballShell } from "@/components/shell/app-shell";
import { TeamAnalytics } from "@/components/team-analytics";
import { Suspense } from "react";

export default function TeamStatsPage() {
  return (
    <FootballShell activePage="team-stats">
      <Suspense fallback={<p role="status">Opening team analysis…</p>}><TeamAnalytics /></Suspense>
    </FootballShell>
  );
}
