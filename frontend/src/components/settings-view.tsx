"use client";

/**
 * Settings.
 *
 * Backend-wired: GET/PATCH /api/v1/settings/system for the team system config
 * (including the auto-process-on-upload switch) and model sensitivity.
 * Settings holds configuration only — pipeline job detail lives in its one
 * home, Film Room → Upload & Process Film.
 */

import { useEffect, useRef, useState } from "react";
import { useAppState } from "@/lib/app-state";
import { getSystemSettings, updateSystemSettings } from "@/lib/api";
import type { SystemSettingsResponse } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { NativeSelect } from "@/components/ui/native-select";
import { Slider } from "@/components/ui/slider";
import { Switch } from "@/components/ui/switch";

export function SettingsView() {
  const { authToken } = useAppState();
  const [settings, setSettings] = useState<SystemSettingsResponse | null>(null);
  const [draft, setDraft] = useState<SystemSettingsResponse | null>(null);
  const [loadState, setLoadState] = useState<"idle" | "loading" | "error">("loading");
  const [loadError, setLoadError] = useState<string | null>(null);
  const [saving, setSaving] = useState<null | "system_config" | "model_sensitivity">(null);
  const [saveErrors, setSaveErrors] = useState<{
    system_config: string | null;
    model_sensitivity: string | null;
  }>({ system_config: null, model_sensitivity: null });
  const [savedSection, setSavedSection] = useState<null | "system_config" | "model_sensitivity">(
    null,
  );
  const savedIndicatorTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    return () => {
      if (savedIndicatorTimeoutRef.current) {
        clearTimeout(savedIndicatorTimeoutRef.current);
        savedIndicatorTimeoutRef.current = null;
      }
    };
  }, []);

  useEffect(() => {
    if (!authToken) {
      setSettings(null);
      setDraft(null);
      setLoadState("idle");
      setLoadError(null);
      setSaving(null);
      setSaveErrors({ system_config: null, model_sensitivity: null });
      setSavedSection(null);
      if (savedIndicatorTimeoutRef.current) {
        clearTimeout(savedIndicatorTimeoutRef.current);
        savedIndicatorTimeoutRef.current = null;
      }
      return;
    }
    let cancelled = false;
    setLoadState("loading");
    setLoadError(null);
    getSystemSettings(authToken)
      .then((s) => {
        if (cancelled) return;
        setSettings(s);
        setDraft(s);
        setLoadState("idle");
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setLoadError(err instanceof Error ? err.message : String(err));
        setLoadState("error");
      });
    return () => {
      cancelled = true;
    };
  }, [authToken]);

  const save = async (section: "system_config" | "model_sensitivity") => {
    if (!authToken || !draft) return;
    setSaving(section);
    setSaveErrors((cur) => ({ ...cur, [section]: null }));
    setSavedSection(null);
    if (savedIndicatorTimeoutRef.current) {
      clearTimeout(savedIndicatorTimeoutRef.current);
      savedIndicatorTimeoutRef.current = null;
    }
    try {
      const updated = await updateSystemSettings({ [section]: draft[section] }, authToken);
      setSettings(updated);
      setDraft(updated);
      setSavedSection(section);
      savedIndicatorTimeoutRef.current = setTimeout(
        () => setSavedSection((cur) => (cur === section ? null : cur)),
        2500,
      );
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      setSaveErrors((cur) => ({ ...cur, [section]: msg }));
    } finally {
      setSaving(null);
    }
  };

  const updateSystemConfig = <K extends keyof SystemSettingsResponse["system_config"]>(
    key: K,
    value: SystemSettingsResponse["system_config"][K],
  ) => {
    setDraft((cur) =>
      cur ? { ...cur, system_config: { ...cur.system_config, [key]: value } } : cur,
    );
  };

  const updateSensitivity = <K extends keyof SystemSettingsResponse["model_sensitivity"]>(
    key: K,
    value: SystemSettingsResponse["model_sensitivity"][K],
  ) => {
    setDraft((cur) =>
      cur ? { ...cur, model_sensitivity: { ...cur.model_sensitivity, [key]: value } } : cur,
    );
  };

  const systemDirty =
    settings != null &&
    draft != null &&
    JSON.stringify(settings.system_config) !== JSON.stringify(draft.system_config);
  const sensitivityDirty =
    settings != null &&
    draft != null &&
    JSON.stringify(settings.model_sensitivity) !== JSON.stringify(draft.model_sensitivity);

  return (
    <div className="grid items-start gap-4 lg:grid-cols-2">
      <Card>
        <CardHeader>
          <h2 className="font-display text-base font-semibold uppercase tracking-wide">
            System Config
          </h2>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          {loadState === "loading" && authToken && (
            <p className="text-xs text-muted-foreground">Loading settings…</p>
          )}
          {loadState === "error" && (
            <p role="alert" className="text-xs text-status-danger">
              {loadError ?? "Failed to load settings."}
            </p>
          )}
          {!authToken && (
            <p className="text-xs text-muted-foreground">
              Sign in to view and edit system settings.
            </p>
          )}
          {draft && (
            <>
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="settings-team-name">Team name</Label>
                <Input
                  id="settings-team-name"
                  value={draft.system_config.team_name}
                  maxLength={120}
                  onChange={(e) => updateSystemConfig("team_name", e.target.value)}
                />
              </div>
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="settings-capture-camera">Capture camera</Label>
                <Input
                  id="settings-capture-camera"
                  value={draft.system_config.capture_camera}
                  maxLength={120}
                  onChange={(e) => updateSystemConfig("capture_camera", e.target.value)}
                />
              </div>
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="settings-storage-bucket">Storage bucket (display only)</Label>
                <Input
                  id="settings-storage-bucket"
                  value={draft.system_config.storage_bucket}
                  maxLength={120}
                  onChange={(e) => updateSystemConfig("storage_bucket", e.target.value)}
                />
              </div>
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="settings-auto-export">Auto-export access</Label>
                <NativeSelect
                  id="settings-auto-export"
                  value={draft.system_config.auto_export_access}
                  onChange={(e) =>
                    updateSystemConfig(
                      "auto_export_access",
                      e.target.value as SystemSettingsResponse["system_config"]["auto_export_access"],
                    )
                  }
                >
                  <option value="off">Off</option>
                  <option value="staff">Staff</option>
                  <option value="all">All users</option>
                </NativeSelect>
              </div>
              <div className="flex items-start justify-between gap-3 rounded-md border border-border-soft p-3">
                <div>
                  <Label htmlFor="auto-process-toggle" className="text-[0.82rem]">
                    Process film automatically on upload
                  </Label>
                  <p className="mt-0.5 text-xs text-muted-foreground">
                    When off, uploads wait for a manual Process Film click in the Film Room.
                  </p>
                </div>
                <Switch
                  id="auto-process-toggle"
                  data-testid="auto-process-toggle"
                  checked={draft.system_config.auto_process_on_upload}
                  onCheckedChange={(checked) =>
                    updateSystemConfig("auto_process_on_upload", checked)
                  }
                />
              </div>
              <div className="flex items-center gap-2">
                <Button
                  onClick={() => save("system_config")}
                  disabled={!systemDirty || saving === "system_config"}
                >
                  {saving === "system_config" ? "Saving…" : "Save"}
                </Button>
                {savedSection === "system_config" && (
                  <span role="status" className="text-xs text-status-ok">
                    Saved
                  </span>
                )}
              </div>
              {saveErrors.system_config && (
                <p role="alert" className="text-xs text-status-danger">
                  {saveErrors.system_config}
                </p>
              )}
            </>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <h2 className="font-display text-base font-semibold uppercase tracking-wide">
            Model Sensitivity
          </h2>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          {draft && (
            <>
              {(
                [
                  ["boundary_sensitivity", "Boundary sensitivity"],
                  ["identity_confidence", "Identity confidence"],
                  ["motion_minimum", "Motion minimum"],
                  ["pose_review_gate", "Pose review gate"],
                ] as const
              ).map(([key, label]) => (
                <div key={key} className="flex flex-col gap-2">
                  <div className="flex items-baseline justify-between gap-2">
                    <Label htmlFor={`sensitivity-${key}`} className="text-[0.82rem]">
                      {label}
                    </Label>
                    <span data-numeric className="font-mono text-xs text-muted-foreground">
                      {draft.model_sensitivity[key]}
                    </span>
                  </div>
                  <Slider
                    id={`sensitivity-${key}`}
                    min={0}
                    max={100}
                    step={1}
                    value={[draft.model_sensitivity[key]]}
                    onValueChange={([value]) => updateSensitivity(key, value)}
                    aria-label={label}
                  />
                </div>
              ))}
              <div className="flex items-center gap-2">
                <Button
                  onClick={() => save("model_sensitivity")}
                  disabled={!sensitivityDirty || saving === "model_sensitivity"}
                >
                  {saving === "model_sensitivity" ? "Saving…" : "Save"}
                </Button>
                {savedSection === "model_sensitivity" && (
                  <span role="status" className="text-xs text-status-ok">
                    Saved
                  </span>
                )}
              </div>
            </>
          )}
          {saveErrors.model_sensitivity && (
            <p role="alert" className="text-xs text-status-danger">
              {saveErrors.model_sensitivity}
            </p>
          )}
          {!authToken && (
            <p className="text-xs text-muted-foreground">Sign in to tune model sensitivity.</p>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
