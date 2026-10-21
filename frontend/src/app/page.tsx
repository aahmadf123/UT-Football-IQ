import { FootballShell } from "@/components/shell/app-shell";
import { TeamDashboard } from "@/components/team-dashboard/team-dashboard";

export default function HomePage() {
  return (
    <FootballShell activePage="dashboard">
      <TeamDashboard />
    </FootballShell>
  );
}
