import { FootballShell } from "@/components/shell/app-shell";
import { SettingsView } from "@/components/settings-view";

export default function SettingsPage() {
  return (
    <FootballShell activePage="settings">
      <SettingsView />
    </FootballShell>
  );
}
