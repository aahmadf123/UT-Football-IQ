/**
 * Categorical series colours, in fixed order (never cycled, never by rank).
 *
 * The dark steps of the validated reference palette, checked against the
 * app's card surface (#0e1823) with the dataviz validator: lightness band,
 * chroma floor, adjacent CVD ΔE 9.4, normal-vision ΔE 26.5, contrast ≥ 3:1.
 * Past three series, facet into small multiples instead of adding hues.
 */
export const SERIES_COLORS = ["#3987e5", "#d95926", "#199e70"] as const;

/** Practice and game keep their colours on every chart, whichever is shown. */
export const SESSION_COLORS: Record<string, string> = {
  practice: SERIES_COLORS[0],
  game: SERIES_COLORS[1],
};

/** The quiet de-emphasis line for sparklines. */
export const MUTED_LINE = "var(--muted-foreground)";
