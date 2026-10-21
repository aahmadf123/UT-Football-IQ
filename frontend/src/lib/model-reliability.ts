/** Strict display contracts: never invent diagnostics for historical records. */
export function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}
export function finite(value: unknown): value is number { return typeof value === "number" && Number.isFinite(value); }
export function count(value: unknown): value is number { return finite(value) && Number.isSafeInteger(value) && value >= 0; }
export function gatePassed(value: unknown) {
  const report = record(value), population = record(report.population);
  if (report.source !== "measured_human_holdout" || report.passed !== true) return false;
  if (report.kind !== "expected_outcomes") return true;
  const plays = record(population.plays_by_category), groups = record(population.groups_by_category);
  return population.passed === true && count(population.canonical_plays) && population.canonical_plays >= 240
    && count(population.independent_groups) && population.independent_groups >= 12
    && population.independent_groups <= population.canonical_plays
    && report.sample_count === population.canonical_plays
    && count(population.double_labeled_adjudicated_plays) && population.double_labeled_adjudicated_plays >= Math.ceil(population.canonical_plays * 0.2)
    && population.double_labeled_adjudicated_plays <= population.canonical_plays
    && ["practice", "coach_game", "television"].every((key) => count(plays[key]) && plays[key] >= 80 && count(groups[key]) && groups[key] >= 4 && groups[key] <= plays[key])
    && ["practice", "coach_game", "television"].reduce((sum, key) => sum + (plays[key] as number), 0) === population.canonical_plays
    && ["practice", "coach_game", "television"].reduce((sum, key) => sum + (groups[key] as number), 0) === population.independent_groups;
}
export function calibration(value: unknown, total: number, binary: boolean) {
  if (!Array.isArray(value) || !value.length || value.length > 100) return null;
  const rows = value.map(record);
  if (rows.some((row) => !count(row.count) || row.count === 0 || !finite(row.predicted_mean) || !finite(row.observed_mean)
    || (binary && (row.predicted_mean < 0 || row.predicted_mean > 1 || row.observed_mean < 0 || row.observed_mean > 1)))
    || rows.reduce((sum, row) => sum + (row.count as number), 0) !== total) return null;
  return rows as { count: number; predicted_mean: number; observed_mean: number }[];
}
export function confusion(value: unknown, total: number) {
  const matrix = record(value), labels = matrix.labels, counts = matrix.counts;
  if (!Array.isArray(labels) || !labels.length || labels.length > 64 || !labels.every((v) => typeof v === "string" && v.length > 0)
    || new Set(labels).size !== labels.length || !Array.isArray(counts) || counts.length !== labels.length
    || counts.some((row) => !Array.isArray(row) || row.length !== labels.length || !row.every(count))
    || counts.flat().reduce((sum, value) => sum + value, 0) !== total
    || matrix.rows !== "observed" || matrix.columns !== "predicted") return null;
  if (matrix.decision_threshold !== undefined && (!finite(matrix.decision_threshold) || matrix.decision_threshold < 0 || matrix.decision_threshold > 1)) return null;
  return { labels: labels as string[], counts: counts as number[][], threshold: matrix.decision_threshold };
}
export function errors(value: unknown, total: number, binary: boolean) {
  const data = record(value), bins = data.bins;
  if (data.definition !== "prediction_minus_observation" || data.interval !== "lower_inclusive_upper_exclusive"
    || data.unit !== (binary ? "probability" : "yd") || !Array.isArray(bins) || !bins.length || bins.length > 100) return null;
  const rows = bins.map(record);
  if (rows.some((row, i) => !count(row.count) || (row.lower !== null && !finite(row.lower)) || (row.upper !== null && !finite(row.upper))
    || (i > 0 && row.lower !== rows[i - 1].upper) || (row.lower === null && i !== 0) || (row.upper === null && i !== rows.length - 1)
    || (finite(row.lower) && finite(row.upper) && row.lower >= row.upper))
    || rows.reduce((sum, row) => sum + (row.count as number), 0) !== total) return null;
  return rows as { lower: number | null; upper: number | null; count: number }[];
}
