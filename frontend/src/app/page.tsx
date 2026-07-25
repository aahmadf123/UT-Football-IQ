import { FootballShell } from "@/components/shell/app-shell";
import { DashboardView } from "@/components/dashboard-view";

export default function DashboardPage() {
  return (
    <FootballShell activePage="dashboard">
      <DashboardView />
    </FootballShell>
  );
}
