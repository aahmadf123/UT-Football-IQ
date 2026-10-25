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

import { useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";
import { createCorrection, type CorrectionCreate } from "@/lib/api";
import { useAppState } from "@/lib/app-state";
import { canSubmitCorrections } from "@/lib/roles";
import type { OverlayTracklet } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { NativeSelect } from "@/components/ui/native-select";
import { friendlyMessage } from "@/lib/api-error";
import { ObservedEvidenceFields, buildEvidenceAttributes, emptyEvidenceDraft, INTERACTION_TYPES } from "./observed-evidence-fields";
import { ObservedOutcomesFields, OUTCOME_TYPES, outcomeAttributes, emptyOutcomeDraft } from "./observed-outcomes-fields";
import { PredictionContextFields, buildPredictionContext, emptyPredictionContext } from "./prediction-context-fields";

type CorrectionKind =
  | "player_identity"
  | "formation_tag"
  | "coverage_tag"
  | "route_tag"
  | "personnel_tag"
  | "event_mark"
  | "position_observation"
  | "clip_boundary"
  | "event_tag";

const KIND_OPTIONS: ReadonlyArray<{ value: CorrectionKind; label: string }> = [
  { value: "player_identity", label: "Player / referee / unit / jersey" },
  { value: "formation_tag", label: "Formation is wrong" },
  { value: "coverage_tag", label: "Coverage is wrong" },
  { value: "route_tag", label: "Route is wrong" },
  { value: "personnel_tag", label: "Personnel grouping is wrong" },
  { value: "event_mark", label: "Mark a play event" },
  { value: "position_observation", label: "Record position observation" },
  { value: "clip_boundary", label: "Play boundary is wrong / bad clip" },
  { value: "event_tag", label: "Bad boxes / detection glitch" },
];

const OBSERVED_ROLES: Record<string, string[]> = {
  offense: ["QB", "RB", "FB", "WR", "TE", "LT", "LG", "C", "RG", "RT"],
  defense: ["DL", "DT", "DE", "EDGE", "LB", "DB", "CB", "S"],
  special_teams: ["K", "P", "LS", "HOLDER", "BALL", "KR", "PR"],
};
const POSITION_OBSERVATIONS = [
  ["position_participation", "Position participation"], ["position_role", "Observed position role"],
  ["first_movement", "First movement"], ["te_usage", "TE block/chip/route state"],
  ["snap", "Snap"], ["kick", "Kick"], ["punt", "Punt"], ["landing", "Ball landing"],
  ["pass_release", "Observed forward-pass release"], ["catch", "Observed catch"],
  ["protection_envelope", "Measured protection envelope"],
  ["receiver_release", "Observed receiver release location"], ["route_break", "Observed route break location"],
  ["pressure_observation", "Observed pressure coverage interval"], ["geometry_observation", "Measured LOS body/occupancy snapshot"],
  ["handoff", "Observed handoff to carrier"], ["possession_start", "Observed carrier possession start"],
  ["first_contact", "Observed first carrier contact"], ["end_of_play", "Observed carrier endpoint"],
  ["tackle", "Observed carrier tackle endpoint"], ["out_of_bounds", "Observed carrier out-of-bounds endpoint"], ["touchdown", "Observed carrier touchdown endpoint"],
  ["engagement_start", "Observed engagement start"], ["engagement_end", "Observed engagement end"],
  ["chip_end", "Observed chip end"], ["route_start", "Observed route start after chip"],
  ["pass_target", "Reviewed forward-pass target"], ["pass_result", "Reviewed pass result"],
  ["reception", "Reviewed offensive reception"], ["possession_end", "Reviewed possession endpoint"],
  ["carrier_cut", "Reviewed carrier cut"], ["tackle_outcome", "Reviewed tackle outcome"],
] as const;
const EMPTY_POSITION = { type: "position_participation", role: "", side: "", opportunity: "routes", end: "", direction: "", opponent: "", shot: "", interaction: "", state: "chip", operation: "", ball: "", x: "", y: "", envelope: "", windowStart: "", los: "" };
const OPPORTUNITY_LABELS: Record<string, string> = { routes: "Route", pass_rush: "Pass rush", pursuit: "Pursuit", coverage: "Coverage", blocking_reps: "Blocking rep", punts: "Punt", kickoffs: "Kickoff", field_goal: "Field goal", rushes: "Rush", targets: "Target", pass_protection: "Pass protection", tackle_opportunities: "Tackle opportunity", returns: "Return" };
function positionOpportunities(side: string, role: string): string[] {
  if (side === "defense" && OBSERVED_ROLES.defense.includes(role)) return ["pass_rush", "pursuit", "coverage", "tackle_opportunities"];
  if (side === "offense" && role === "WR") return ["routes", "targets", "rushes"];
  if (side === "offense" && role === "TE") return ["routes", "blocking_reps", "targets", "rushes", "pass_protection"];
  if (side === "offense" && ["RB", "FB"].includes(role)) return ["rushes", "targets", "pass_protection"];
  if (side === "offense" && role === "QB") return ["rushes"];
  if (side === "offense" && ["LT", "LG", "C", "RG", "RT"].includes(role)) return ["pass_protection"];
  if (side === "special_teams" && role === "P") return ["punts"];
  if (side === "special_teams" && role === "K") return ["kickoffs", "field_goal"];
  if (side === "special_teams" && ["KR", "PR"].includes(role)) return ["returns"];
  return [];
}
function knownShot(shot: unknown): shot is string {
  return typeof shot === "string" && !!shot.trim() && !["unknown", "unverified", "unresolved", "none", "null", "missing", "n/a"].includes(shot.trim().toLowerCase());
}

type SubmitStatus =
  | { kind: "idle" }
  | { kind: "sent" }
  | { kind: "error"; message: string };

interface Props {
  clipId: string;
  /** Tracklets from the overlays payload (may be empty while ingest runs). */
  tracklets: OverlayTracklet[];
  /**
   * Tracklet picked by clicking its box on the overlay canvas. Each click
   * produces a fresh object, so re-selecting the same tracklet still re-opens
   * the panel with the identity form prefilled for that track.
   */
  selection?: { trackletId: string } | null;
  annotationRevision?: number;
  clipBounds?: { start: number; end: number; duration: number | null };
  onSaved?: () => void;
  currentTimeSeconds?: number;
  getCurrentTimeSeconds?: () => number;
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

export function CorrectionsPanel({ clipId, tracklets, selection, annotationRevision, clipBounds, onSaved, currentTimeSeconds = 0, getCurrentTimeSeconds }: Props) {
  const { authToken, currentRole, data, refreshData } = useAppState();
  const locked = !canSubmitCorrections(currentRole);

  const [open, setOpen] = useState(false);
  const [kind, setKind] = useState<CorrectionKind>("player_identity");
  const [trackletId, setTrackletId] = useState("");
  const [team, setTeam] = useState("");
  const [jersey, setJersey] = useState("");
  const [playerId, setPlayerId] = useState("");
  const [actorKind, setActorKind] = useState("");
  const selectedTrack = tracklets.find((track) => track.id === trackletId);
  const effectiveActor = actorKind || selectedTrack?.provenance?.actor_kind || (["official", "sideline"].includes(selectedTrack?.team_label ?? "") ? selectedTrack?.team_label : undefined);
  const nonPlayer = effectiveActor === "official" || effectiveActor === "sideline" || effectiveActor === "unknown";

  // Clicking a player box on the video jumps straight into "who is this?".
  // Depends on the selection object's identity, not just the tracklet id, so
  // clicking the same box again re-opens a hidden panel.
  useEffect(() => {
    if (!selection) return;
    setOpen(true);
    setKind("player_identity");
    setTrackletId(selection.trackletId);
    setActorKind(""); setPlayerId(""); setTeam(""); setJersey("");
    setStatus({ kind: "idle" });
  }, [selection]);

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
  const [eventType, setEventType] = useState("snap");
  const [eventTime, setEventTime] = useState("");
  const [position, setPosition] = useState(EMPTY_POSITION);
  const [vertices, setVertices] = useState([{ x: "", y: "" }, { x: "", y: "" }, { x: "", y: "" }]);
  const [evidenceDraft, setEvidenceDraft] = useState(emptyEvidenceDraft);
  const [outcomeDraft, setOutcomeDraft] = useState(emptyOutcomeDraft);
  const [predictionDraft, setPredictionDraft] = useState(emptyPredictionContext);
  const knownShots = [...new Set(tracklets.flatMap((track) => track.track_points.map((point) => point.shot_id).filter(knownShot)))];
  const [startSeconds, setStartSeconds] = useState("");
  const [endSeconds, setEndSeconds] = useState("");
  const [note, setNote] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [status, setStatus] = useState<SubmitStatus>({ kind: "idle" });
  const operationRef = useRef<{ fingerprint: string; id: string } | null>(null);

  const clearForm = () => {
    setTrackletId("");
    setTeam("");
    setJersey("");
    setPlayerId("");
    setActorKind("");
    setFormation("");
    setEventTime("");
    setPosition(EMPTY_POSITION);
    setVertices([{ x: "", y: "" }, { x: "", y: "" }, { x: "", y: "" }]);
    setEvidenceDraft(emptyEvidenceDraft());
    setOutcomeDraft(emptyOutcomeDraft());
    setPredictionDraft(emptyPredictionContext());
    setStartSeconds("");
    setEndSeconds("");
    setNote("");
  };

  const buildPayload = (eventTimeNow = currentTimeSeconds): CorrectionCreate | null => {
    const trimmedNote = note.trim();
    switch (kind) {
      case "player_identity": {
        if (!trackletId) return null;
        const corrected: Record<string, unknown> = { tracklet_id: trackletId };
        if (actorKind) corrected.actor_kind = actorKind;
        if (team && !nonPlayer) corrected.team = team;
        const rosterPlayer = playerId && !nonPlayer ? data.players.find((p) => p.id === playerId) : undefined;
        if (rosterPlayer) {
          // A roster pick is the strongest identity signal: carries the
          // player id (and their jersey, when numeric) for the re-ID loop.
          corrected.player_id = rosterPlayer.id;
          const rosterJersey = Number(rosterPlayer.jersey);
          if (Number.isFinite(rosterJersey)) corrected.jersey_number = rosterJersey;
        } else if (!nonPlayer && jersey.trim() !== "" && Number.isInteger(Number(jersey)) && Number(jersey) >= 0 && Number(jersey) <= 99) {
          corrected.jersey_number = Number(jersey);
        }
        // Require an actual fix, not just a tracklet reference.
        if (!("actor_kind" in corrected) && !("team" in corrected) && !("jersey_number" in corrected) && !("player_id" in corrected)) {
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
      case "formation_tag":
      case "coverage_tag":
      case "route_tag":
      case "personnel_tag": {
        if (!formation.trim()) return null;
        return {
          clip_id: clipId,
          correction_type: kind,
          corrected_value: { [kind.replace("_tag", "")]: formation.trim() },
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
        if (!("start_seconds" in corrected) && !("end_seconds" in corrected)) return null;
        const start = typeof corrected.start_seconds === "number" ? corrected.start_seconds : clipBounds?.start;
        const end = typeof corrected.end_seconds === "number" ? corrected.end_seconds : clipBounds?.end;
        if ((start != null && start < 0) || (end != null && end < 0) || (start != null && end != null && start >= end) || (end != null && clipBounds?.duration != null && end > clipBounds.duration)) return null;
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
      case "event_mark": {
        const time = eventTime.trim() === "" ? eventTimeNow : Number(eventTime);
        if (!Number.isFinite(time) || time < 0 || (clipBounds && time > clipBounds.end - clipBounds.start)) return null;
        return { clip_id: clipId, correction_type: "event_tag", corrected_value: { event_type: eventType, timestamp_seconds: time }, notes: trimmedNote || null };
      }
      case "position_observation": {
        const time = eventTime.trim() === "" ? eventTimeNow : Number(eventTime);
        const actor = tracklets.find((track) => track.id === trackletId);
        const shot = position.shot || (knownShots.length === 1 ? knownShots[0] : "");
        if ((!actor && !["protection_envelope", "geometry_observation"].includes(position.type)) || !shot || !knownShots.includes(shot) || !Number.isFinite(time) || time < 0 || (clipBounds && time > clipBounds.end - clipBounds.start)) return null;
        const attributes: Record<string, unknown> = { ...(actor ? { actor_id: actor.id } : {}), shot_id: shot };
        const declaresRole = ["position_role", "position_participation"].includes(position.type);
        if (declaresRole) {
          if (!OBSERVED_ROLES[position.side]?.includes(position.role)) return null;
          Object.assign(attributes, { observed_role: position.role, team_label: position.side });
        }
        if (position.type === "position_participation") {
          if (!positionOpportunities(position.side, position.role).includes(position.opportunity)) return null;
          Object.assign(attributes, { opportunity_kind: position.opportunity });
          if (position.end.trim()) {
            const end = Number(position.end);
            if (!Number.isFinite(end) || end <= time || (clipBounds && end > clipBounds.end - clipBounds.start)) return null;
            Object.assign(attributes, { end_seconds: end, interval_timebase: "clip" });
          }
          if (position.direction) attributes.attacking_direction = Number(position.direction);
        }
        if (position.type === "snap") {
          if (position.los.trim()) {
            if (!Number.isFinite(Number(position.los)) || Number(position.los) < 0 || Number(position.los) > 100) return null;
            attributes.los_x = Number(position.los);
          }
          if (position.direction) attributes.attacking_direction = Number(position.direction);
        }
        if (position.opponent && ["position_participation", "te_usage", ...INTERACTION_TYPES, ...OUTCOME_TYPES].includes(position.type)) {
          if (position.opponent === actor?.id || !tracklets.some((track) => track.id === position.opponent)) return null;
          attributes.opponent_id = position.opponent;
        }
        if (position.type === "te_usage") {
          if (!position.interaction.trim() || (position.state !== "route" && !position.opponent)) return null;
          Object.assign(attributes, { state: position.state, interaction_id: position.interaction.trim() });
        }
        if (["kick", "punt", "landing"].includes(position.type) && !position.operation.trim()) return null;
        if (position.operation.trim()) attributes.operation_id = position.operation.trim();
        if (position.ball) {
          if (!tracklets.some((track) => track.id === position.ball)) return null;
          attributes.ball_id = position.ball;
        }
        if (["landing", "receiver_release", "route_break"].includes(position.type)) {
          if ((position.type === "landing" && !position.ball) || !position.x.trim() || !position.y.trim() || !Number.isFinite(Number(position.x)) || !Number.isFinite(Number(position.y))) return null;
          Object.assign(attributes, { field_x: Number(position.x), field_y: Number(position.y), visibility_state: "observed" });
        }
        if (position.type === "protection_envelope") {
          if (!position.envelope.trim() || vertices.some((vertex) => !vertex.x.trim() || !vertex.y.trim() || !Number.isFinite(Number(vertex.x)) || !Number.isFinite(Number(vertex.y)))) return null;
          const polygon = vertices.map((vertex) => ({ field_x: Number(vertex.x), field_y: Number(vertex.y) }));
          if (polygon.some((point) => point.field_x < -10 || point.field_x > 110 || Math.abs(point.field_y) > 26.665)) return null;
          const signedArea = polygon.reduce((sum, point, index) => { const next = polygon[(index + 1) % polygon.length]; return sum + point.field_x * next.field_y - next.field_x * point.field_y; }, 0) / 2;
          if (Math.abs(signedArea) < 0.000001) return null;
          Object.assign(attributes, { envelope_id: position.envelope.trim(), polygon, visibility_state: "observed" });
          if (position.windowStart.trim() || position.end.trim()) {
            const start = Number(position.windowStart), end = Number(position.end);
            if (!position.windowStart.trim() || !position.end.trim() || !Number.isFinite(start) || !Number.isFinite(end) || start < 0 || start > time || end < time || start >= end || (clipBounds && end > clipBounds.end - clipBounds.start)) return null;
            Object.assign(attributes, { start_seconds: start, end_seconds: end, interval_timebase: "clip" });
          }
        }
        const observed = buildEvidenceAttributes(position.type, trackletId, time, evidenceDraft,
          tracklets, position.opponent, position.interaction, clipBounds ? clipBounds.end - clipBounds.start : undefined);
        if (observed === null) return null;
        Object.assign(attributes, observed);
        const outcome = outcomeAttributes(position.type, outcomeDraft, position.opponent);
        if (outcome === null) return null;
        Object.assign(attributes, outcome);
        const context = buildPredictionContext({ type: position.type, value: predictionDraft, tracks: tracklets, actor: trackletId,
          localTime: time, clipStart: clipBounds?.start, shot, originalRoles: evidenceDraft.originalRoles, los: position.los, direction: position.direction });
        if (context === null) return null;
        Object.assign(attributes, context.attributes);
        return { clip_id: clipId, correction_type: "event_tag", corrected_value: { event_type: position.type,
          timestamp_seconds: context.sourceTimestamp ?? time, timebase: context.sourceTimestamp == null ? "clip" : "recording",
          ...(context.frameNumber == null ? {} : { frame_number: context.frameNumber }), attributes }, notes: trimmedNote || null };
      }
    }
  };

  const payload = buildPayload();

  const submit = async () => {
    const submitPayload = buildPayload(getCurrentTimeSeconds?.() ?? currentTimeSeconds);
    if (locked || submitting) return;
    if (!submitPayload) {
      setStatus({ kind: "error", message: predictionDraft.enabled
        ? "The observation no longer matches a supported source frame. Review the fields and choose a prediction source frame, then save again."
        : "The observation is incomplete or changed while saving. Review the fields, then save again." });
      return;
    }
    setSubmitting(true);
    setStatus({ kind: "idle" });
    try {
      const fingerprint = JSON.stringify({ payload: submitPayload, annotationRevision });
      if (operationRef.current?.fingerprint !== fingerprint) operationRef.current = { fingerprint, id: crypto.randomUUID() };
      await createCorrection({ ...submitPayload, expected_revision: annotationRevision, client_operation_id: operationRef.current.id }, authToken);
      setStatus({ kind: "sent" });
      toast.success("Correction saved", {
        description: "Applied to this play and available for the learning loop.",
      });
      clearForm();
      operationRef.current = null;
      onSaved?.();
      void refreshData();
    } catch (err) {
      const message = friendlyMessage(err);
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
                setFormation("");
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
                    onChange={(e) => { setTrackletId(e.target.value); setActorKind(""); setPlayerId(""); setTeam(""); setJersey(""); }}
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
              <Field htmlFor="correction-person-kind" label="Person kind">
                <NativeSelect id="correction-person-kind" value={actorKind} disabled={disabled} onChange={(event) => { setActorKind(event.target.value); setPlayerId(""); setTeam(""); setJersey(""); }}>
                  <option value="">Leave unchanged</option><option value="player">Player</option><option value="official">Referee / official</option><option value="sideline">Staff / sideline person</option><option value="unknown">Unknown person</option>
                </NativeSelect>
                {nonPlayer && <p className="text-xs text-muted-foreground">Non-players are kept separate from roster identities and player analysis.</p>}
              </Field>
              <Field htmlFor="correction-player" label="Correct player (roster)">
                <NativeSelect
                  id="correction-player"
                  data-testid="correction-player-select"
                  value={playerId}
                  disabled={disabled || nonPlayer}
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
                  disabled={disabled || nonPlayer}
                  onChange={(e) => setTeam(e.target.value)}
                >
                  <option value="">Leave unchanged</option>
                  <option value="home">Jersey group 1</option>
                  <option value="away">Jersey group 2</option>
                  <option value="offense">Observed offense</option>
                  <option value="defense">Observed defense</option>
                  <option value="special_teams">Observed special teams</option>
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
                    disabled={disabled || nonPlayer}
                    onChange={(e) => setJersey(e.target.value)}
                  />
                </Field>
              )}
            </>
          )}

          {kind === "coverage_tag" && <Field htmlFor="correction-coverage" label="Correct coverage"><NativeSelect id="correction-coverage" value={formation} disabled={disabled} onChange={(event) => setFormation(event.target.value)}><option value="">Choose coverage…</option>{[["cover_0", "Cover 0"], ["cover_1", "Cover 1"], ["cover_2_mof", "Cover 2 MOF"], ["cover_2_shell", "Cover 2 shell"], ["cover_3", "Cover 3"], ["cover_4", "Cover 4"], ["man_free", "Man free"], ["bracket_match", "Bracket / match"], ["cover_6", "Cover 6"]].map(([value, label]) => <option key={value} value={value}>{label}</option>)}</NativeSelect></Field>}

          {["formation_tag", "route_tag", "personnel_tag"].includes(kind) && (
            <Field htmlFor="correction-formation" label={`Correct ${kind.replace("_tag", "")}`}>
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

          {kind === "event_mark" && <div className="grid grid-cols-2 gap-2">
            <Field htmlFor="correction-event-type" label="Event"><NativeSelect id="correction-event-type" value={eventType} disabled={disabled} onChange={(event) => setEventType(event.target.value)}>{["snap", "throw", "handoff", "catch", "play_end"].map((value) => <option key={value} value={value}>{value.replace("_", " ")}</option>)}</NativeSelect></Field>
            <Field htmlFor="correction-event-time" label="Play time (seconds)"><Input id="correction-event-time" type="number" min={0} step="0.1" value={eventTime} placeholder={currentTimeSeconds.toFixed(1)} disabled={disabled} onChange={(event) => setEventTime(event.target.value)} /></Field>
          </div>}

          {kind === "position_observation" && <>
            <p className="text-xs text-muted-foreground">Record only what this play shows. Player identity is corrected separately. Link related observations using the same interaction or kicking operation name.</p>
            <Field htmlFor="position-observation" label="Observation"><NativeSelect id="position-observation" value={position.type} disabled={disabled} onChange={(event) => { setPosition({ ...position, type: event.target.value }); setOutcomeDraft(emptyOutcomeDraft()); setPredictionDraft(emptyPredictionContext()); }}>{POSITION_OBSERVATIONS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</NativeSelect></Field>
            <Field htmlFor="position-actor" label={position.type === "protection_envelope" ? "Observed QB track (optional)" : position.type === "geometry_observation" ? "Observed player track (optional)" : "Observed player track"}><NativeSelect id="position-actor" value={trackletId} disabled={disabled} onChange={(event) => setTrackletId(event.target.value)}><option value="">Select a track…</option>{tracklets.map((track, index) => <option key={track.id} value={track.id}>T{index + 1}</option>)}</NativeSelect></Field>
            <Field htmlFor="position-shot" label="Observed camera shot"><NativeSelect id="position-shot" value={position.shot || (knownShots.length === 1 ? knownShots[0] : "")} disabled={disabled} onChange={(event) => setPosition({ ...position, shot: event.target.value })}><option value="">Select the shot containing this observation…</option>{knownShots.map((shot, index) => <option key={shot} value={shot}>Shot {index + 1}</option>)}</NativeSelect></Field>
            {knownShots.length === 0 && <p className="text-xs text-muted-foreground">No source shot is available. Finish tracking review before recording this observation.</p>}
            <Field htmlFor="position-time" label="Observation time (play seconds)"><Input id="position-time" type="number" min={0} step="0.01" value={eventTime} placeholder={currentTimeSeconds.toFixed(2)} disabled={disabled} onChange={(event) => setEventTime(event.target.value)} /></Field>
            {["position_role", "position_participation"].includes(position.type) && <>
              <p className="text-xs text-muted-foreground">Name original line roles at the snap; pulling or crossing does not rename them.</p>
              <Field htmlFor="position-side" label="Side of the ball"><NativeSelect id="position-side" value={position.side} disabled={disabled} onChange={(event) => setPosition({ ...position, side: event.target.value, role: "" })}><option value="">Select observed side…</option><option value="offense">Offense</option><option value="defense">Defense</option><option value="special_teams">Special teams</option></NativeSelect></Field>
              <Field htmlFor="position-role" label="Observed role"><NativeSelect id="position-role" value={position.role} disabled={disabled} onChange={(event) => setPosition({ ...position, role: event.target.value })}><option value="">Select observed role…</option>{(OBSERVED_ROLES[position.side] ?? []).map((role) => <option key={role} value={role}>{role}</option>)}</NativeSelect></Field>
            </>}
            {position.type === "position_participation" && <>
              <Field htmlFor="position-opportunity" label="Opportunity"><NativeSelect id="position-opportunity" value={positionOpportunities(position.side, position.role).includes(position.opportunity) ? position.opportunity : ""} disabled={disabled} onChange={(event) => setPosition({ ...position, opportunity: event.target.value })}><option value="">Select observed participation…</option>{positionOpportunities(position.side, position.role).map((value) => <option key={value} value={value}>{OPPORTUNITY_LABELS[value]}</option>)}</NativeSelect></Field>
              <Field htmlFor="position-end" label="Participation end (play seconds)"><Input id="position-end" type="number" min={0} step="0.01" value={position.end} disabled={disabled} onChange={(event) => setPosition({ ...position, end: event.target.value })} /></Field>
              <p className="text-xs text-muted-foreground">An end time enables interval geometry. An unknown end still records explicit participation.</p>
              <Field htmlFor="position-direction" label="Attacking direction"><NativeSelect id="position-direction" value={position.direction} disabled={disabled} onChange={(event) => setPosition({ ...position, direction: event.target.value })}><option value="">Unknown</option><option value="1">Toward increasing field x</option><option value="-1">Toward decreasing field x</option></NativeSelect></Field>
            </>}
            {position.type === "snap" && <>
              <Field htmlFor="position-los" label="Measured LOS field x (yards)"><Input id="position-los" type="number" min={0} max={100} step="0.01" value={position.los} disabled={disabled} onChange={(event) => setPosition({ ...position, los: event.target.value })} /></Field>
              <Field htmlFor="snap-direction" label="Attacking direction"><NativeSelect id="snap-direction" value={position.direction} disabled={disabled} onChange={(event) => setPosition({ ...position, direction: event.target.value })}><option value="">Unknown</option><option value="1">Toward increasing field x</option><option value="-1">Toward decreasing field x</option></NativeSelect></Field>
            </>}
            {["position_participation", "te_usage", ...INTERACTION_TYPES, ...OUTCOME_TYPES].includes(position.type) && <Field htmlFor="position-opponent" label="Comparison player (optional)"><NativeSelect id="position-opponent" value={position.opponent} disabled={disabled} onChange={(event) => setPosition({ ...position, opponent: event.target.value })}><option value="">No explicit comparison</option>{tracklets.filter((track) => track.id !== trackletId).map((track) => <option key={track.id} value={track.id}>T{tracklets.indexOf(track) + 1}</option>)}</NativeSelect></Field>}
            {position.type === "te_usage" && <>
              <Field htmlFor="position-state" label="Observed TE state"><NativeSelect id="position-state" value={position.state} disabled={disabled} onChange={(event) => setPosition({ ...position, state: event.target.value })}>{["block", "chip", "route"].map((state) => <option key={state} value={state}>{state}</option>)}</NativeSelect></Field>
              <Field htmlFor="position-interaction" label="Interaction name"><Input id="position-interaction" value={position.interaction} disabled={disabled} onChange={(event) => setPosition({ ...position, interaction: event.target.value })} /></Field>
            </>}
            {INTERACTION_TYPES.includes(position.type) && <Field htmlFor="paired-interaction" label="Interaction name"><Input id="paired-interaction" value={position.interaction} disabled={disabled} onChange={(event) => setPosition({ ...position, interaction: event.target.value })} /></Field>}
            <ObservedOutcomesFields type={position.type} value={outcomeDraft} onChange={setOutcomeDraft} disabled={disabled} />
            <ObservedEvidenceFields type={position.type} tracks={tracklets} value={evidenceDraft} onChange={setEvidenceDraft} disabled={disabled} />
            <PredictionContextFields type={position.type} tracks={tracklets} actor={trackletId} value={predictionDraft} onChange={setPredictionDraft} disabled={disabled} clipStart={clipBounds?.start} clipEnd={clipBounds?.end} onClockChange={(localTime) => setEventTime(String(localTime))} />
            {predictionDraft.enabled && !payload && <p role="status" className="text-xs text-muted-foreground">Prediction context is incomplete or conflicts with the selected source frame. Review the required players, complete populations, known context times and original roles. Every selected player must be visible in the same exact native frame; no missing value is estimated.</p>}
            {["snap", "kick", "punt", "landing"].includes(position.type) && <>
              <Field htmlFor="position-operation" label="Kicking operation name"><Input id="position-operation" value={position.operation} disabled={disabled} onChange={(event) => setPosition({ ...position, operation: event.target.value })} /></Field>
              <Field htmlFor="position-ball" label="Observed ball track"><NativeSelect id="position-ball" value={position.ball} disabled={disabled} onChange={(event) => setPosition({ ...position, ball: event.target.value })}><option value="">Unknown / unavailable</option>{tracklets.filter((track) => track.id !== trackletId).map((track) => <option key={track.id} value={track.id}>T{tracklets.indexOf(track) + 1}</option>)}</NativeSelect></Field>
            </>}
            {position.type === "landing" && <div className="grid grid-cols-2 gap-2">
              <Field htmlFor="position-x" label="Measured landing field x (yards)"><Input id="position-x" type="number" step="0.01" value={position.x} disabled={disabled} onChange={(event) => setPosition({ ...position, x: event.target.value })} /></Field>
              <Field htmlFor="position-y" label="Measured landing field y (yards)"><Input id="position-y" type="number" step="0.01" value={position.y} disabled={disabled} onChange={(event) => setPosition({ ...position, y: event.target.value })} /></Field>
            </div>}
            {["receiver_release", "route_break"].includes(position.type) && <div className="grid grid-cols-2 gap-2">
              <Field htmlFor="actor-location-x" label="Measured actor field x (yards)"><Input id="actor-location-x" type="number" step="0.01" value={position.x} disabled={disabled} onChange={(event) => setPosition({ ...position, x: event.target.value })} /></Field>
              <Field htmlFor="actor-location-y" label="Measured actor field y (yards)"><Input id="actor-location-y" type="number" step="0.01" value={position.y} disabled={disabled} onChange={(event) => setPosition({ ...position, y: event.target.value })} /></Field>
            </div>}
            {position.type === "protection_envelope" && <>
              <p className="text-xs text-muted-foreground">Enter measured vertices in boundary order around the field polygon. Use the same envelope name and measurement window for each observed source time. This does not grade protection success.</p>
              <Field htmlFor="envelope-name" label="Envelope name"><Input id="envelope-name" value={position.envelope} disabled={disabled} onChange={(event) => setPosition({ ...position, envelope: event.target.value })} /></Field>
              <Field htmlFor="envelope-start" label="Measurement window start (play seconds)"><Input id="envelope-start" type="number" min={0} step="0.01" value={position.windowStart} disabled={disabled} onChange={(event) => setPosition({ ...position, windowStart: event.target.value })} /></Field>
              <Field htmlFor="envelope-end" label="Measurement window end (play seconds)"><Input id="envelope-end" type="number" min={0} step="0.01" value={position.end} disabled={disabled} onChange={(event) => setPosition({ ...position, end: event.target.value })} /></Field>
              {vertices.map((vertex, index) => <div key={index} className="grid grid-cols-2 gap-2">
                <Field htmlFor={`vertex-${index}-x`} label={`Envelope vertex ${index + 1} field x (yards)`}><Input id={`vertex-${index}-x`} type="number" step="0.01" value={vertex.x} disabled={disabled} onChange={(event) => setVertices(vertices.map((item, i) => i === index ? { ...item, x: event.target.value } : item))} /></Field>
                <Field htmlFor={`vertex-${index}-y`} label={`Envelope vertex ${index + 1} field y (yards)`}><Input id={`vertex-${index}-y`} type="number" step="0.01" value={vertex.y} disabled={disabled} onChange={(event) => setVertices(vertices.map((item, i) => i === index ? { ...item, y: event.target.value } : item))} /></Field>
              </div>)}
              <div className="flex gap-2"><Button type="button" variant="outline" size="sm" disabled={disabled || vertices.length >= 16} onClick={() => setVertices([...vertices, { x: "", y: "" }])}>Add vertex</Button><Button type="button" variant="outline" size="sm" disabled={disabled || vertices.length <= 3} onClick={() => setVertices(vertices.slice(0, -1))}>Remove last vertex</Button></div>
            </>}
          </>}

          {kind === "clip_boundary" && (
            <>
            <p className="text-xs text-muted-foreground">Use the original recording timeline.{clipBounds ? ` Current play: ${clipBounds.start.toFixed(1)}–${clipBounds.end.toFixed(1)} seconds.` : ""}</p>
            <div className="flex gap-2">
              <Field htmlFor="correction-start" label="Recording start (s)" className="flex-1">
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
              <Field htmlFor="correction-end" label="Recording end (s)" className="flex-1">
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
            </>
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
            {submitting ? "Saving…" : "Save correction"}
          </Button>

          {status.kind === "sent" && (
            <p data-testid="correction-success" className="text-xs text-status-ok">
              Saved to this play. Available for the learning loop.
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
