import { expect, test } from "vitest";
import { canonicalShotId } from "./shot-id";

test("shot identifiers preserve zero, integer strings and opaque IDs without numeric rounding", () => {
  for (const [input, expected] of [[0, "0"], [1, "1"], ["00", "0"], [" 001 ", "1"], ["００１", "1"], ["١", "1"], ["9007199254740993", "9007199254740993"], [" shot-a ", "shot-a"]] as const) {
    expect(canonicalShotId(input)).toBe(expected);
  }
});

test("unresolved continuity and noninteger numeric values have no known shot", () => {
  for (const input of [undefined, null, true, false, -1, .5, Infinity, NaN, Number.MAX_SAFE_INTEGER + 1, {}, "", "  ", "Unknown", "unverified", " unresolved ", "missing", "N/A", "none", "null"]) {
    expect(canonicalShotId(input)).toBeNull();
  }
});
