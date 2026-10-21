const UNKNOWN_SHOTS = new Set(["unknown", "unverified", "unresolved", "none", "null", "missing", "n/a"]);
const DECIMAL_DIGIT = /^\p{Decimal_Number}$/u;
const DECIMAL_STRING = /^\p{Decimal_Number}+$/u;

/** Match worker canonical_shot: zero is valid; unknown continuity has no ID. */
export function canonicalShotId(value: unknown): string | null {
  if (typeof value === "number") return Number.isSafeInteger(value) && value >= 0 ? String(value) : null;
  if (typeof value !== "string") return null;
  const text = value.trim();
  if (!text || UNKNOWN_SHOTS.has(text.toLowerCase())) return null;
  if (!DECIMAL_STRING.test(text)) return text;
  // Python str.isdecimal/int also accept decimal scripts, without rounding IDs.
  let digits = "";
  for (const character of text) {
    const code = character.codePointAt(0)!;
    let first = code;
    while (first > 0 && DECIMAL_DIGIT.test(String.fromCodePoint(first - 1))) first -= 1;
    digits += String((code - first) % 10);
  }
  return digits.replace(/^0+(?=\d)/, "");
}
