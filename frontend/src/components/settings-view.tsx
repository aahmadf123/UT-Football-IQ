"use client";

import { useCallback, useEffect, useState } from "react";
import { useAppState } from "@/lib/app-state";
import { getSystemSettings, updateSystemSettings } from "@/lib/api";
import type { SystemSettingsResponse } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { LocalSystemPanel } from "@/components/local-system-panel";
import { LearningPanel } from "@/components/learning-panel";

export function SettingsView() {
  const { authToken, currentRole } = useAppState();
  const [settings, setSettings] = useState<SystemSettingsResponse | null>(null);
  const [name, setName] = useState("");
  const [automatic, setAutomatic] = useState(true);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const canEdit = currentRole === "admin";

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await getSystemSettings(authToken);
      setSettings(response);
      setName(response.system_config.team_name);
      setAutomatic(response.system_config.auto_process_on_upload);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setLoading(false);
    }
  }, [authToken]);

  useEffect(() => { void load(); }, [load]);

  async function save() {
    if (!canEdit || !name.trim()) return;
    setSaving(true);
    setError(null);
    setSaved(false);
    try {
      const response = await updateSystemSettings({ system_config: { team_name: name.trim(), auto_process_on_upload: automatic } }, authToken);
      setSettings(response);
      setName(response.system_config.team_name);
      setAutomatic(response.system_config.auto_process_on_upload);
      setSaved(true);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setSaving(false);
    }
  }

  const dirty = settings !== null && (name.trim() !== settings.system_config.team_name || automatic !== settings.system_config.auto_process_on_upload);

  return (
    <div className="flex flex-col gap-5">
      <LocalSystemPanel />
      <Card>
        <CardHeader>
          <h2 className="text-base font-semibold">Library preferences</h2>
          <p className="text-sm text-muted-foreground">These settings apply to the library on this computer.</p>
        </CardHeader>
        <CardContent className="flex max-w-2xl flex-col gap-4">
          {loading ? <p role="status">Loading preferences…</p> : null}
          {error ? <div role="alert" className="rounded-lg border border-status-danger/40 p-3 text-sm"><p>{error}</p><Button variant="outline" size="sm" onClick={() => void load()} className="mt-2">Try again</Button></div> : null}
          {settings ? <>
            <div className="flex flex-col gap-2">
              <Label htmlFor="settings-team-name">Team name</Label>
              <Input id="settings-team-name" value={name} maxLength={120} disabled={!canEdit || saving} onChange={(event) => { setName(event.target.value); setSaved(false); }} />
            </div>
            <div className="flex items-start justify-between gap-4 rounded-lg border border-border-soft p-4">
              <div>
                <Label htmlFor="auto-process-toggle">Process film automatically on upload</Label>
                <p className="mt-2 text-sm leading-relaxed text-muted-foreground">When enabled, new recordings enter the local processing queue. When disabled, choose Process film in Film Room.</p>
              </div>
              <Switch id="auto-process-toggle" data-testid="auto-process-toggle" checked={automatic} disabled={!canEdit || saving} onCheckedChange={(checked) => { setAutomatic(checked); setSaved(false); }} />
            </div>
            {canEdit ? <div className="flex items-center gap-3"><Button onClick={() => void save()} disabled={saving || !dirty || !name.trim()}>{saving ? "Saving…" : "Save preferences"}</Button>{saved ? <span role="status" className="text-sm text-status-ok">Saved</span> : null}</div> : <p className="text-sm text-muted-foreground">An administrator can change library preferences.</p>}
          </> : null}
        </CardContent>
      </Card>
      <LearningPanel />
    </div>
  );
}
