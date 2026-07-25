import { FootballShell } from "@/components/shell/app-shell";
import { PlayersView } from "@/components/players-view";

export default function PlayersPage() {
  return (
    <FootballShell activePage="players">
      <PlayersView />
    </FootballShell>
  );
}
