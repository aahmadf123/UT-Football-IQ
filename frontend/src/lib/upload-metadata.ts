/**
 * Session metadata captured at upload time.
 *
 * This is the piece that was missing. `addUploads` has always accepted an
 * `UploadMetadata` argument and forwarded it correctly to `POST /videos`, but
 * no caller ever passed one -- so every video landed with `session_kind`,
 * `opponent_team`, `recorded_at` and `our_possession` all null. That single
 * omission is what makes the Library's opponent and possession filters match
 * nothing, and what leaves the entire Opponent Prep tab permanently empty
 * (the backend derives its opponent list from `videos.opponent_team`).
 */

import type { OurPossession, SessionKind, SourceType } from "@/lib/types";

export interface UploadMetadataDraft {
  /** ISO-8601 instant the film was recorded. */
  recordedAt: string | null;
  sessionKind: SessionKind | null;
  sourceType: SourceType | null;
  /** Only meaningful for `game`; cleared otherwise. */
  opponentTeam: string | null;
  ourPossession: OurPossession | null;
  tags: string[];
}

export const EMPTY_DRAFT: UploadMetadataDraft = {
  recordedAt: null,
  sessionKind: null,
  sourceType: null,
  opponentTeam: null,
  ourPossession: null,
  tags: [],
};

export const SESSION_KINDS: { value: SessionKind; label: string }[] = [
  { value: "practice", label: "Practice" },
  { value: "scrimmage", label: "Scrimmage" },
  { value: "game", label: "Game" },
];

export const POSSESSIONS: { value: OurPossession; label: string }[] = [
  { value: "offense", label: "Offense" },
  { value: "defense", label: "Defense" },
  { value: "special_teams", label: "Special teams" },
];

export const SOURCE_TYPES: { value: SourceType; label: string }[] = [
  { value: "drone", label: "Drone" },
  { value: "uploaded_clip", label: "Other camera" },
];

/**
 * Pull the capture instant out of a DJI filename.
 *
 * DJI writes `DJI_YYYYMMDDHHMMSS_NNNN_D.MP4`, and the Toledo practice footage
 * follows it with spaces instead of underscores (`Dji 20260416110000 0257 D.mp4`).
 * Both are handled, since which one you get depends on how the card was copied.
 *
 * Returns an ISO string, or null when the filename carries no usable stamp --
 * a wrong date silently attached to a practice is worse than no date, so
 * anything that does not parse cleanly as a real calendar instant is rejected.
 */
export function parseCaptureDate(filename: string): string | null {
  // 14 digits, not part of a longer run, in either separator style.
  const match = /(?:^|[^0-9])(\d{14})(?:[^0-9]|$)/.exec(filename);
  if (!match) return null;

  const stamp = match[1];
  const year = Number(stamp.slice(0, 4));
  const month = Number(stamp.slice(4, 6));
  const day = Number(stamp.slice(6, 8));
  const hour = Number(stamp.slice(8, 10));
  const minute = Number(stamp.slice(10, 12));
  const second = Number(stamp.slice(12, 14));

  // A plausible-looking number is not a date: 20261340999999 parses field by
  // field but is nonsense, and Date would roll it over into a real instant.
  if (year < 2000 || year > 2100) return null;
  if (month < 1 || month > 12) return null;
  if (day < 1 || day > 31) return null;
  if (hour > 23 || minute > 59 || second > 59) return null;

  // Local time: the stamp is what the drone's clock read at the field, and the
  // coach thinks in local practice times.
  const date = new Date(year, month - 1, day, hour, minute, second);

  // Round-trip check catches rollover (e.g. Feb 31 -> Mar 3).
  if (date.getMonth() !== month - 1 || date.getDate() !== day) return null;

  return date.toISOString();
}

/**
 * Seed a draft from the dropped files.
 *
 * The date comes from the earliest parseable filename, so a batch covering one
 * practice gets that practice's date rather than whichever file the browser
 * happened to list first. `sourceType` defaults to drone when the names look
 * like DJI output.
 */
export function draftFromFiles(files: readonly File[]): UploadMetadataDraft {
  const dates = files.map((f) => parseCaptureDate(f.name)).filter((d): d is string => d !== null);
  dates.sort();

  const looksLikeDrone = files.some((f) => /^dji[\s_]/i.test(f.name));

  return {
    ...EMPTY_DRAFT,
    recordedAt: dates[0] ?? null,
    sourceType: looksLikeDrone ? "drone" : null,
  };
}

/** Field-level problems that should block submission. */
export function validateDraft(draft: UploadMetadataDraft): Record<string, string> {
  const errors: Record<string, string> = {};
  if (!draft.sessionKind) {
    errors.sessionKind = "Pick practice, scrimmage, or game.";
  }
  if (!draft.recordedAt) {
    errors.recordedAt = "Pick the date this film was shot.";
  }
  if (draft.sessionKind === "game" && !draft.opponentTeam?.trim()) {
    // Without this, the film is invisible to Opponent Prep, which is the whole
    // reason to tag a game.
    errors.opponentTeam = "Game film needs an opponent so it reaches Opponent Prep.";
  }
  return errors;
}

/**
 * Convert a draft to the wire shape `POST /videos` expects.
 *
 * `opponent_team` is dropped for non-game film: the backend derives the
 * opponent list from that column, and a practice tagged with an opponent would
 * invent a matchup that never happened.
 */
export function draftToMetadata(draft: UploadMetadataDraft): {
  recorded_at?: string | null;
  session_kind?: SessionKind | null;
  source_type?: SourceType | null;
  opponent_team?: string | null;
  our_possession?: OurPossession | null;
  tags?: string[];
} {
  const opponent = draft.sessionKind === "game" ? (draft.opponentTeam?.trim() || null) : null;
  return {
    recorded_at: draft.recordedAt,
    session_kind: draft.sessionKind,
    source_type: draft.sourceType,
    opponent_team: opponent,
    our_possession: draft.ourPossession,
    tags: draft.tags.length ? draft.tags : undefined,
  };
}

/** Split a comma/enter-separated tag input into clean, de-duplicated tags. */
export function parseTags(input: string): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const raw of input.split(",")) {
    const tag = raw.trim().toLowerCase();
    if (!tag || seen.has(tag)) continue;
    seen.add(tag);
    out.push(tag);
  }
  return out;
}

/** Format an ISO instant for a `datetime-local` input. */
export function toLocalInputValue(iso: string | null): string {
  if (!iso) return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  const pad = (n: number) => String(n).padStart(2, "0");
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` +
    `T${pad(date.getHours())}:${pad(date.getMinutes())}`
  );
}

/** Parse a `datetime-local` value back to an ISO instant. */
export function fromLocalInputValue(value: string): string | null {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date.toISOString();
}
