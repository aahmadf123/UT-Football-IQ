import { FootballShell } from "@/components/shell/app-shell";
import { HealthWorkloadView } from "@/components/health-workload-view";

export default function HealthWorkloadPage() {
  return (
    <FootballShell activePage="health-workload">
      <HealthWorkloadView />
    </FootballShell>
  );
}
