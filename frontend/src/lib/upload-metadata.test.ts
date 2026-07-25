import { describe, expect, it } from "vitest";
import {
  draftFromFiles,
  draftToMetadata,
  fromLocalInputValue,
  parseCaptureDate,
  parseTags,
  toLocalInputValue,
  validateDraft,
  excessTagCount,
  EMPTY_DRAFT,
  MAX_TAGS,
} from "./upload-metadata";

function file(name: string): File {
  return new File([new Uint8Array(4)], name, { type: "video/mp4" });
}

describe("parseCaptureDate", () => {
  it("parses the Toledo drone footage naming exactly as shipped", () => {
    // This is the literal shape of the files in Drone Footage/.
    const iso = parseCaptureDate("Dji 20260416110000 0257 D.mp4");
    expect(iso).not.toBeNull();
    const date = new Date(iso!);
    expect(date.getFullYear()).toBe(2026);
    expect(date.getMonth()).toBe(3); // April
    expect(date.getDate()).toBe(16);
    expect(date.getHours()).toBe(11);
    expect(date.getMinutes()).toBe(0);
  });

  it("parses DJI's underscore form too", () => {
    // Which separator you get depends on how the card was copied.
    const spaced = parseCaptureDate("Dji 20260416110000 0257 D.mp4");
    const underscored = parseCaptureDate("DJI_20260416110000_0257_D.MP4");
    expect(underscored).toBe(spaced);
  });

  it("keeps the time of day, which is what separates one period from the next", () => {
    const a = new Date(parseCaptureDate("Dji 20260416110000 0257 D.mp4")!);
    const b = new Date(parseCaptureDate("Dji 20260416111511 0283 D.mp4")!);
    expect(b.getTime()).toBeGreaterThan(a.getTime());
    expect(b.getHours()).toBe(11);
    expect(b.getMinutes()).toBe(15);
  });

  it.each([
    ["no digits", "practice.mp4"],
    ["too few digits", "clip 2026041611000 x.mp4"],
    ["month 13", "Dji 20261316110000 0257 D.mp4"],
    ["day 32", "Dji 20260432110000 0257 D.mp4"],
    ["hour 24", "Dji 20260416240000 0257 D.mp4"],
    ["minute 60", "Dji 20260416116000 0257 D.mp4"],
    ["implausible year", "Dji 19260416110000 0257 D.mp4"],
  ])("returns null for %s", (_label, name) => {
    // A wrong date silently attached to a practice is worse than no date.
    expect(parseCaptureDate(name)).toBeNull();
  });

  it("rejects a date that only exists after rollover", () => {
    // Feb 31 would become Mar 3 if handed straight to Date.
    expect(parseCaptureDate("Dji 20260231110000 0257 D.mp4")).toBeNull();
  });

  it("does not match a longer digit run", () => {
    // A 15-digit id is not a timestamp; matching its first 14 would invent one.
    expect(parseCaptureDate("clip202604161100001.mp4")).toBeNull();
  });
});

describe("draftFromFiles", () => {
  it("uses the earliest capture in the batch, not file order", () => {
    // The browser lists files in whatever order it likes; a practice batch
    // should carry that practice's start.
    const draft = draftFromFiles([
      file("Dji 20260416111511 0283 D.mp4"),
      file("Dji 20260416110000 0257 D.mp4"),
      file("Dji 20260416110504 0266 D.mp4"),
    ]);
    expect(new Date(draft.recordedAt!).getMinutes()).toBe(0);
  });

  it("infers drone source from DJI filenames", () => {
    expect(draftFromFiles([file("Dji 20260416110000 0257 D.mp4")]).sourceType).toBe("drone");
  });

  it("leaves source unset for anything else", () => {
    expect(draftFromFiles([file("sideline-cam.mp4")]).sourceType).toBeNull();
  });

  it("survives a batch with no parseable names", () => {
    const draft = draftFromFiles([file("a.mp4"), file("b.mp4")]);
    expect(draft.recordedAt).toBeNull();
    expect(draft.sessionKind).toBeNull();
  });

  it("handles an empty batch", () => {
    expect(draftFromFiles([])).toEqual(EMPTY_DRAFT);
  });
});

describe("validateDraft", () => {
  it("requires a session kind and a date", () => {
    const errors = validateDraft(EMPTY_DRAFT);
    expect(errors.sessionKind).toBeTruthy();
    expect(errors.recordedAt).toBeTruthy();
  });

  it("requires an opponent on game film", () => {
    // Opponent Prep derives its team list from videos.opponent_team, so game
    // film without one is invisible there — the whole point of tagging it.
    const errors = validateDraft({
      ...EMPTY_DRAFT,
      sessionKind: "game",
      recordedAt: new Date().toISOString(),
    });
    expect(errors.opponentTeam).toBeTruthy();
  });

  it("does not require an opponent on practice film", () => {
    const errors = validateDraft({
      ...EMPTY_DRAFT,
      sessionKind: "practice",
      recordedAt: new Date().toISOString(),
    });
    expect(errors).toEqual({});
  });

  it("treats a whitespace-only opponent as missing", () => {
    const errors = validateDraft({
      ...EMPTY_DRAFT,
      sessionKind: "game",
      recordedAt: new Date().toISOString(),
      opponentTeam: "   ",
    });
    expect(errors.opponentTeam).toBeTruthy();
  });
});

describe("draftToMetadata", () => {
  it("sends the fields the Library and Opponent Prep filter on", () => {
    const wire = draftToMetadata({
      recordedAt: "2026-04-16T15:00:00.000Z",
      sessionKind: "game",
      sourceType: "drone",
      opponentTeam: "Ball State",
      ourPossession: "offense",
      tags: ["red-zone"],
    });
    expect(wire).toMatchObject({
      recorded_at: "2026-04-16T15:00:00.000Z",
      session_kind: "game",
      source_type: "drone",
      opponent_team: "Ball State",
      our_possession: "offense",
      tags: ["red-zone"],
    });
  });

  it("drops the opponent on non-game film", () => {
    // A practice tagged with an opponent would invent a matchup that never
    // happened, and the backend derives its opponent list from that column.
    const wire = draftToMetadata({
      ...EMPTY_DRAFT,
      sessionKind: "practice",
      opponentTeam: "Ball State",
    });
    expect(wire.opponent_team).toBeNull();
  });

  it("trims the opponent", () => {
    const wire = draftToMetadata({
      ...EMPTY_DRAFT,
      sessionKind: "game",
      opponentTeam: "  Ball State  ",
    });
    expect(wire.opponent_team).toBe("Ball State");
  });

  it("omits tags entirely when there are none", () => {
    expect(draftToMetadata(EMPTY_DRAFT).tags).toBeUndefined();
  });
});

describe("parseTags", () => {
  it("splits, trims, lowercases and de-duplicates", () => {
    expect(parseTags("Red Zone, red zone ,  Third Down,")).toEqual(["red zone", "third down"]);
  });

  it("returns nothing for an empty input", () => {
    expect(parseTags("   ")).toEqual([]);
  });
});

describe("datetime-local round trip", () => {
  it("survives a round trip to the minute", () => {
    const iso = new Date(2026, 3, 16, 11, 5, 0).toISOString();
    const roundTripped = fromLocalInputValue(toLocalInputValue(iso));
    expect(new Date(roundTripped!).getMinutes()).toBe(5);
    expect(new Date(roundTripped!).getHours()).toBe(11);
  });

  it("renders local wall-clock time, not UTC", () => {
    // The coach thinks in "11am practice", whatever the offset is.
    const iso = new Date(2026, 3, 16, 11, 0, 0).toISOString();
    expect(toLocalInputValue(iso)).toBe("2026-04-16T11:00");
  });

  it.each([null, "", "not-a-date"])("returns empty for %s", (value) => {
    expect(toLocalInputValue(value as string | null)).toBe("");
  });

  it("returns null for an unparseable input value", () => {
    expect(fromLocalInputValue("nonsense")).toBeNull();
  });
});

describe("tag cap", () => {
  it("splits on newlines as well as commas", () => {
    // Pasting a list is the common way to get newlines in here.
    expect(parseTags("red zone\nthird down,install")).toEqual([
      "red zone",
      "third down",
      "install",
    ]);
  });

  it("reports the overflow rather than silently dropping tags", () => {
    // Truncating would stop the 422 but lose the coach's labels without
    // saying so; validateDraft blocks and explains instead.
    const many = Array.from({ length: 25 }, (_, i) => `tag-${i}`).join(",");
    const tags = parseTags(many);
    expect(tags).toHaveLength(25);
    expect(excessTagCount(many)).toBe(5);
  });

  it("blocks submission when over the cap", () => {
    // Caught before upload: a batch that 422s on register leaves every object
    // orphaned in R2, and the retry repeats the same invalid metadata.
    const errors = validateDraft({
      ...EMPTY_DRAFT,
      sessionKind: "practice",
      recordedAt: new Date().toISOString(),
      tags: Array.from({ length: 21 }, (_, i) => `tag-${i}`),
    });
    expect(errors.tags).toContain("At most 20");
  });

  it("allows exactly the cap", () => {
    const errors = validateDraft({
      ...EMPTY_DRAFT,
      sessionKind: "practice",
      recordedAt: new Date().toISOString(),
      tags: Array.from({ length: MAX_TAGS }, (_, i) => `tag-${i}`),
    });
    expect(errors.tags).toBeUndefined();
  });
});
