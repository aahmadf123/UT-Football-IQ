import { FootballShell } from "@/components/shell/app-shell";
import { GameSummaryView } from "@/components/game-summary-view";

export default function TeamStatsPage() {
  return (
    <FootballShell activePage="team-stats">
      <GameSummaryView />
    </FootballShell>
  );
}
