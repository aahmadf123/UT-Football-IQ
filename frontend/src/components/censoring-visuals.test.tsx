import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { CensoringVisuals, suppliedCensoringGroups, type CensoringGroup } from "./censoring-visuals";

const options = vi.hoisted(() => ({ value: {} as { series?: { data: number[][] }[] } }));
vi.mock("@/components/charts/echart", () => ({ EChart: ({ option, label, onSelect }: { option: typeof options.value; label: string; onSelect: (index: number) => void }) => { options.value = option; return <button onClick={() => onSelect(1)}>{label}</button>; } }));
afterEach(cleanup);
const row = { play_id: "play", clip_id: "clip", metric_id: "duration", value: 3, video_id: "video", start_seconds: 2, end_seconds: 5, right_censored: true };
const group: CensoringGroup = { key: "pressure", metric_name: "time_to_first_pressure", definition_version: "v1", unit: "s", n: 1, events: 0, censored: 1, evidence: [row], survival_curve: [{ seconds: 0, at_risk: 1, events: 0, censored: 0, survival: 1 }, { seconds: 3, at_risk: 1, events: 0, censored: 1, survival: 1 }] };

test("missing curves and incomplete evidence produce no invented visualization", () => {
  const { container } = render(<CensoringVisuals onSelect={vi.fn()} />);
  expect(container.childElementCount).toBe(0);
  expect(suppliedCensoringGroups([{ ...group, survival_curve: [] }])).toHaveLength(0);
  expect(suppliedCensoringGroups([{ ...group, evidence: [] }])).toHaveLength(0);
});

test("an entirely censored cohort keeps supplied survival values and lower bounds", () => {
  render(<CensoringVisuals groups={[group]} onSelect={vi.fn()} />);
  expect(options.value.series?.[0].data).toEqual([[0, 1], [3, 1]]);
  expect(screen.getByText("≥ 3.00")).toBeTruthy();
  expect(screen.getByText(/n=1 qualified duration observations/)).toBeTruthy();
  expect(screen.getByText("Right-censored lower bound")).toBeTruthy();
});

test("curve selection and accessible evidence open an actual qualified source row", () => {
  const select = vi.fn();
  render(<CensoringVisuals groups={[group]} onSelect={select} />);
  fireEvent.click(screen.getByRole("button", { name: /time to first pressure survival/ }));
  expect(select).toHaveBeenLastCalledWith(row);
  fireEvent.click(screen.getByRole("button", { name: "Inspect duration" }));
  expect(select).toHaveBeenLastCalledWith(row);
});

test("nonfinite, increasing survival, inconsistent counts and absent definitions fail closed", () => {
  for (const patch of [{ definition_version: "" }, { n: 2 }, { unit: "yd" }, { metric_name: "time_to_throw" }, { events: 1 }, { survival_curve: [group.survival_curve[0], { ...group.survival_curve[1], at_risk: 0 }] }, { survival_curve: [group.survival_curve[0], { ...group.survival_curve[1], survival: Number.NaN }] }, { survival_curve: [group.survival_curve[0], { ...group.survival_curve[1], survival: 1.1 }] }]) {
    expect(suppliedCensoringGroups([{ ...group, ...patch }])).toHaveLength(0);
  }
});

test("mixed censoring preserves the backend curve instead of estimating from rows", () => {
  const mixed = { ...group, n: 2, events: 1, evidence: [row, { ...row, metric_id: "observed", value: 1, right_censored: false }], survival_curve: [{ seconds: 0, at_risk: 2, events: 0, censored: 0, survival: 1 }, { seconds: 1, at_risk: 2, events: 1, censored: 0, survival: .5 }, { seconds: 3, at_risk: 1, events: 0, censored: 1, survival: .5 }] };
  render(<CensoringVisuals groups={[mixed]} onSelect={vi.fn()} />);
  expect(options.value.series?.[0].data).toEqual([[0, 1], [1, .5], [3, .5]]);
  expect(screen.getByText("Observed endpoint")).toBeTruthy();
});
