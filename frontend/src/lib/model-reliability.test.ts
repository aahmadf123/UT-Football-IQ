import { expect, test } from "vitest";
import { gatePassed } from "./model-reliability";

function population() { return { source: "measured_human_holdout", kind: "expected_outcomes", passed: true, sample_count: 240, population: { passed: true, canonical_plays: 240, independent_groups: 12, double_labeled_adjudicated_plays: 48, plays_by_category: { practice: 80, coach_game: 80, television: 80 }, groups_by_category: { practice: 4, coach_game: 4, television: 4 } } }; }
test("passing population display requires coherent exact independent sample counts", () => {
  expect(gatePassed(population())).toBe(true);
  for (const mutate of [
    (v: ReturnType<typeof population>) => { v.sample_count = 20; },
    (v: ReturnType<typeof population>) => { v.population.double_labeled_adjudicated_plays = 241; },
    (v: ReturnType<typeof population>) => { v.population.plays_by_category.practice = 81; },
    (v: ReturnType<typeof population>) => { v.population.groups_by_category.practice = 5; },
    (v: ReturnType<typeof population>) => { v.population.independent_groups = 13; },
    (v: ReturnType<typeof population>) => { v.population.groups_by_category.practice = 81; },
    (v: ReturnType<typeof population>) => { v.population.canonical_plays = 240.5; },
  ]) { const value = population(); mutate(value); expect(gatePassed(value)).toBe(false); }
});
