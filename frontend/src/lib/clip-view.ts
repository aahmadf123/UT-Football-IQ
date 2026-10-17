import type { ApiClip } from "./types";

export type ClipView = "sideline" | "endzone";

const VIEW_LABELS: Record<ClipView, string> = { sideline: "Sideline", endzone: "End zone" };

/** Camera angle of a game-film clip, from the segmenter's label_data. */
export function clipView(clip: Pick<ApiClip, "label_data">): ClipView | null {
  const view = clip.label_data?.view;
  return view === "sideline" || view === "endzone" ? view : null;
}

export function viewLabel(view: ClipView): string {
  return VIEW_LABELS[view];
}

/** The other camera angles of the same play, in segment order. */
export function sameplayAngles(clip: ApiClip, siblings: ApiClip[]): ApiClip[] {
  if (clip.play_number == null || !clipView(clip)) return [];
  return siblings
    .filter((c) => c.play_number === clip.play_number && clipView(c) !== null)
    .sort((a, b) => a.start_time - b.start_time);
}

const ORDINALS = ["", "1st", "2nd", "3rd", "4th"];

/** "1st & 10 · Q1 15:00 · Ball on 25" from the play's scoreboard reading. */
export function situationLabel(clip: Pick<ApiClip, "down" | "distance" | "label_data">): string | null {
  const board = (clip.label_data?.scoreboard ?? null) as Record<string, unknown> | null;
  const parts: string[] = [];
  const down = clip.down !== undefined
    ? clip.down
    : typeof board?.down === "number" ? board.down : null;
  if (down != null && down >= 1 && down <= 4) {
    const distance = clip.distance !== undefined
      ? clip.distance
      : typeof board?.distance === "number" ? board.distance : null;
    const goal = typeof distance !== "number" && board?.goal_to_go === true;
    parts.push(`${ORDINALS[down]} & ${goal ? "Goal" : distance ?? "?"}`);
  }
  if (typeof board?.quarter === "number") {
    const quarter = board.quarter === 5 ? "OT" : `Q${board.quarter}`;
    parts.push(typeof board.clock === "string" ? `${quarter} ${board.clock}` : quarter);
  }
  if (typeof board?.ball_on === "number") parts.push(`Ball on ${board.ball_on}`);
  return parts.length ? parts.join(" · ") : null;
}
