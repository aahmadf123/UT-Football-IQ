import { FootballShell } from "@/components/shell/app-shell";
import { PlayerDevelopmentView } from "@/components/player-development-view";

export default function PlayerDevelopmentPage() {
  return (
    <FootballShell activePage="player-development">
      <PlayerDevelopmentView />
    </FootballShell>
  );
}
