import { FootballShell } from "@/components/shell/app-shell";
import { ReportsView } from "@/components/reports-view";

export default function ReportsPage() {
  return (
    <FootballShell activePage="reports">
      <ReportsView />
    </FootballShell>
  );
}
