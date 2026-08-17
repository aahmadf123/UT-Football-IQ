"use client";

/**
 * Coach corrections side panel (Hudl-lite labeling, Phase 3).
 *
 * Deliberately light: a collapsible form in the Clip Review sidebar offering
 * the practical subset of correction types — no drawing tools. Successful
 * submissions POST to ``/api/v1/corrections`` and feed the nightly learning
 * loop; the backend enforces coach-or-above (viewer/player accounts get a
 * 403), so the panel disables its inputs for those roles and says why instead
 * of letting the request fail.
 *
 * Tracklet references use the same ``T{n}`` tags the overlay canvas draws
 * next to each track marker, so a coach can point at the box they mean.
 */

import { useEffect, useMemo, useState } from "react";
import { toast } from "sonner";
import { createCorrection, type CorrectionCreate } from "@/lib/api";
import { useAppState } from "@/lib/app-state";
import { canSubmitCorrections } from "@/lib/roles";
import type { OverlayTracklet } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { NativeSelect } from "@/components/ui/native-select";

type CorrectionKind =
  | "player_identity"
  | "formation_tag"
  | "clip_boundary"
  | "event_tag";

const KIND_OPTIONS: ReadonlyArray<{ value: CorrectionKind; label: string }> = [
  { value: "player_identity", label: "Wrong team / jersey number" },
  { value: "formation_tag", label: "Formation is wrong" },
  { value: "clip_boundary", label: "Play boundary is wrong / bad clip" },
  { value: "event_tag", label: "Bad boxes / detection glitch" },
];

type SubmitStatus =
  | { kind: "idle" }
  | { kind: "sent" }
  | { kind: "error"; message: string };

interface Props {
  clipId: string;
  /** Tracklets from the overlays payload (may be empty while ingest runs). */
  tracklets: OverlayTracklet[];
  /**
   * Tracklet picked by clicking its box on the overlay canvas. A change
   * auto-opens the panel with the identity form prefilled for that track.
   */
  selectedTrackletId?: string | null;
}

function Field({
  htmlFor,
  label,
  children,
  className,
}: {
  htmlFor: string;
  label: string;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <div className={`flex flex-col gap-1 ${className ?? ""}`}>
      <Label htmlFor={htmlFor} className="text-xs">
        {label}
      </Label>
      {children}
    </div>
  );
}

export function CorrectionsPanel({ clipId, tracklets, selectedTrackletId }: Props) {
  const { authToken, currentRole, data } = useAppState();
  const locked = !canSubmitCorrections(currentRole);

  const [open, setOpen] = useState(false);
  const [kind, setKind] = useState<CorrectionKind>("player_identity");
  const [trackletId, setTrackletId] = useState("");
  const [team, setTeam] = useState("");
  const [jersey, setJersey] = useState("");
  const [playerId, setPlayerId] = useState("");

  // Clicking a player box on the video jumps straight into "who is this?".
  useEffect(() => {
    if (!selectedTrackletId) return;
    setOpen(true);
    setKind("player_identity");
    setTrackletId(selectedTrackletId);
    setStatus({ kind: "idle" });
  }, [selectedTrackletId]);

  // Roster options grouped by position group, "#7 · CB · C. Jones" — the
  // group disambiguates duplicate jersey numbers across sides of the ball.
  const rosterGroups = useMemo(() => {
    const groups = new Map<string, typeof data.players>();
    for (const p of data.players) {
      const list = groups.get(p.group) ?? [];
      list.push(p);
      groups.set(p.group, list);
    }
    return [...groups.entries()].sort(([a], [b]) => a.localeCompare(b));
  }, [data.players]);
  const [formation, setFormation] = useState("");
  const [startSeconds, setStartSeconds] = useState("");
  const [endSeconds, setEndSeconds] = useState("");
  const [note, setNote] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [status, setStatus] = useState<SubmitStatus>({ kind: "idle" });

  const clearForm = () => {
    setTrackletId("");
    setTeam("");
    setJersey("");
    setPlayerId("");
    setFormation("");
    setStartSeconds("");
    setEndSeconds("");
    setNote("");
  };

  const buildPayload = (): CorrectionCreate | null => {
    const trimmedNote = note.trim();
    switch (kind) {
      case "player_identity": {
        if (!trackletId) return null;
        const corrected: Record<string, unknown> = { tracklet_id: trackletId };
        if (team) corrected.team = team;
        const rosterPlayer = playerId ? data.players.find((p) => p.id === playerId) : undefined;
        if (rosterPlayer) {
          // A roster pick is the strongest identity signal: carries the
          // player id (and their jersey, when numeric) for the re-ID loop.
          corrected.player_id = rosterPlayer.id;
          const rosterJersey = Number(rosterPlayer.jersey);
          if (Number.isFinite(rosterJersey)) corrected.jersey_number = rosterJersey;
        } else if (jersey.trim() !== "" && Number.isFinite(Number(jersey))) {
          corrected.jersey_number = Number(jersey);
        }
        // Require an actual fix, not just a tracklet reference.
        if (!("team" in corrected) && !("jersey_number" in corrected) && !("player_id" in corrected)) {
          return null;
        }
        const tracklet = tracklets.find((t) => t.id === trackletId);
        return {
          clip_id: clipId,
          correction_type: "player_identity",
          corrected_value: corrected,
          original_value: tracklet
            ? { tracklet_id: tracklet.id, team: tracklet.team_label }
            : null,
          notes: trimmedNote || null,
        };
      }
      case "formation_tag": {
        if (!formation.trim()) return null;
        return {
          clip_id: clipId,
          correction_type: "formation_tag",
          corrected_value: { formation: formation.trim() },
          notes: trimmedNote || null,
        };
      }
      case "clip_boundary": {
        const corrected: Record<string, unknown> = {};
        if (trimmedNote) corrected.note = trimmedNote;
        if (startSeconds.trim() !== "" && Number.isFinite(Number(startSeconds))) {
          corrected.start_seconds = Number(startSeconds);
        }
        if (endSeconds.trim() !== "" && Number.isFinite(Number(endSeconds))) {
          corrected.end_seconds = Number(endSeconds);
        }
        if (Object.keys(corrected).length === 0) return null;
        return {
          clip_id: clipId,
          correction_type: "clip_boundary",
          corrected_value: corrected,
          notes: trimmedNote || null,
        };
      }
      case "event_tag": {
        const corrected: Record<string, unknown> = { issue: "bad_boxes" };
        if (trimmedNote) corrected.note = trimmedNote;
        return {
          clip_id: clipId,
          correction_type: "event_tag",
          corrected_value: corrected,
          notes: trimmedNote || null,
        };
      }
    }
  };

  const payload = buildPayload();

  const submit = async () => {
    if (!payload || locked || submitting) return;
    setSubmitting(true);
    setStatus({ kind: "idle" });
    try {
      await createCorrection(payload, authToken);
      setStatus({ kind: "sent" });
      toast.success("Correction sent", {
        description: "Feeds the nightly learning loop.",
      });
      clearForm();
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      setStatus({ kind: "error", message });
      toast.error("Correction failed", { description: message });
    } finally {
      setSubmitting(false);
    }
  };

  const disabled = locked || submitting;

  return (
    <div data-testid="corrections-panel" className="mt-4">
      <div className="flex items-center justify-between gap-2">
        <h3 className="font-display text-xs font-semibold uppercase tracking-widest text-muted-foreground">
          Corrections
        </h3>
        <Button
          type="button"
          variant="outline"
          size="sm"
          data-testid="corrections-toggle"
          aria-expanded={open}
          onClick={() => setOpen((o) => !o)}
        >
          {open ? "Hide" : "Fix something"}
        </Button>
      </div>

      {open && (
        <div className="mt-2 flex flex-col gap-2.5">
          {locked && (
            <p data-testid="corrections-role-blocked" className="text-xs text-muted-foreground">
              Your role can&apos;t submit corrections. Ask a coach or analyst to file it.
            </p>
          )}

          <Field htmlFor="correction-kind" label="What needs fixing?">
            <NativeSelect
              id="correction-kind"
              value={kind}
              disabled={disabled}
              onChange={(e) => {
                setKind(e.target.value as CorrectionKind);
                setStatus({ kind: "idle" });
              }}
            >
              {KIND_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </NativeSelect>
          </Field>

          {kind === "player_identity" && (
            <>
              {tracklets.length === 0 ? (
                <p className="text-xs text-muted-foreground" data-testid="corrections-no-tracklets">
                  No tracklets on this clip yet — overlays may still be processing.
                </p>
              ) : (
                <Field htmlFor="correction-tracklet" label="Player track (T# on the overlay)">
                  <NativeSelect
                    id="correction-tracklet"
                    value={trackletId}
                    disabled={disabled}
                    onChange={(e) => setTrackletId(e.target.value)}
                  >
                    <option value="">Select a track…</option>
                    {tracklets.map((t, i) => (
                      <option key={t.id} value={t.id}>
                        {`T${i + 1} · ${t.team_label ?? "unknown team"}${
                          t.position_group ? ` · ${t.position_group}` : ""
                        }`}
                      </option>
                    ))}
                  </NativeSelect>
                </Field>
              )}
              <Field htmlFor="correction-player" label="Correct player (roster)">
                <NativeSelect
                  id="correction-player"
                  data-testid="correction-player-select"
                  value={playerId}
                  disabled={disabled}
                  onChange={(e) => setPlayerId(e.target.value)}
                >
                  <option value="">Not on roster / unknown</option>
                  {rosterGroups.map(([group, players]) => (
                    <optgroup key={group} label={group}>
                      {players.map((p) => (
                        <option key={p.id} value={p.id}>
                          {`#${p.jersey} · ${p.position} · ${p.name}`}
                        </option>
                      ))}
                    </optgroup>
                  ))}
                </NativeSelect>
              </Field>
              <Field htmlFor="correction-team" label="Correct team">
                <NativeSelect
                  id="correction-team"
                  value={team}
                  disabled={disabled}
                  onChange={(e) => setTeam(e.target.value)}
                >
                  <option value="">Leave unchanged</option>
                  <option value="home">Home (us)</option>
                  <option value="away">Away (opponent)</option>
                </NativeSelect>
              </Field>
              {!playerId && (
                <Field htmlFor="correction-jersey" label="Correct jersey # (if not on roster)">
                  <Input
                    id="correction-jersey"
                    type="number"
                    min={0}
                    max={99}
                    value={jersey}
                    disabled={disabled}
                    onChange={(e) => setJersey(e.target.value)}
                  />
                </Field>
              )}
            </>
          )}

          {kind === "formation_tag" && (
            <Field htmlFor="correction-formation" label="Correct formation">
              <Input
                id="correction-formation"
                type="text"
                maxLength={80}
                placeholder="e.g. trips right"
                value={formation}
                disabled={disabled}
                onChange={(e) => setFormation(e.target.value)}
              />
            </Field>
          )}

          {kind === "clip_boundary" && (
            <div className="flex gap-2">
              <Field htmlFor="correction-start" label="Start (s)" className="flex-1">
                <Input
                  id="correction-start"
                  type="number"
                  min={0}
                  step="0.1"
                  value={startSeconds}
                  disabled={disabled}
                  onChange={(e) => setStartSeconds(e.target.value)}
                />
              </Field>
              <Field htmlFor="correction-end" label="End (s)" className="flex-1">
                <Input
                  id="correction-end"
                  type="number"
                  min={0}
                  step="0.1"
                  value={endSeconds}
                  disabled={disabled}
                  onChange={(e) => setEndSeconds(e.target.value)}
                />
              </Field>
            </div>
          )}

          <Field
            htmlFor="correction-note"
            label={
              kind === "clip_boundary" || kind === "event_tag" ? "What's wrong?" : "Note (optional)"
            }
          >
            <Input
              id="correction-note"
              type="text"
              maxLength={280}
              value={note}
              disabled={disabled}
              onChange={(e) => setNote(e.target.value)}
            />
          </Field>

          <Button
            type="button"
            size="sm"
            className="w-fit"
            data-testid="correction-submit"
            disabled={disabled || payload == null}
            onClick={submit}
          >
            {submitting ? "Sending…" : "Send correction"}
          </Button>

          {status.kind === "sent" && (
            <p data-testid="correction-success" className="text-xs text-status-ok">
              Sent — feeds the nightly learning loop.
            </p>
          )}
          {status.kind === "error" && (
            <p data-testid="correction-error" className="text-xs text-status-danger">
              {status.message}
            </p>
          )}
        </div>
      )}
    </div>
  );
}
