// AnalyticsCard gated state: callers may attach a coach-readable
// ``gatedReason`` (e.g. the calibration reason from the overlays payload)
// without breaking existing call sites that pass only the generic reason.
import { afterEach, describe, expect, test } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { AnalyticsCard } from "./analytics-card";

afterEach(() => {
  cleanup();
});

describe("AnalyticsCard gated state", () => {
  test("renders the generic reason without gatedReason (backward compatible)", () => {
    render(
      <AnalyticsCard
        title="Spatial Metrics"
        state={{ kind: "gated", reason: "Hidden for this footage." }}
      />,
    );
    expect(screen.getByTestId("card-gated").textContent).toContain(
      "Hidden for this footage.",
    );
    expect(screen.queryByTestId("card-gated-reason")).toBeNull();
  });

  test("renders the coach-readable gatedReason under the generic reason", () => {
    render(
      <AnalyticsCard
        title="Spatial Metrics"
        state={{ kind: "gated", reason: "Hidden for this footage." }}
        gatedReason="Not enough yard lines were visible to map the field, so spatial metrics are hidden for this footage."
      />,
    );
    expect(screen.getByTestId("card-gated-reason").textContent).toContain(
      "Not enough yard lines were visible",
    );
    expect(screen.getByTestId("card-gated").textContent).toContain(
      "Hidden for this footage.",
    );
  });

  test("gatedReason is ignored outside the gated state", () => {
    render(
      <AnalyticsCard
        title="Spatial Metrics"
        state={{ kind: "live" }}
        gatedReason="Should not appear."
      >
        <p>live content</p>
      </AnalyticsCard>,
    );
    expect(screen.queryByTestId("card-gated-reason")).toBeNull();
    expect(screen.getByText("live content")).toBeTruthy();
  });
});
