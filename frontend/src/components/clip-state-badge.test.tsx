import { afterEach, describe, expect, test } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import {
  ClipStateBadges,
  PreliminaryBadge,
  ReviewStateBadge,
} from "./clip-state-badge";

afterEach(() => cleanup());

describe("PreliminaryBadge", () => {
  test("renders the preliminary pill with an explanatory aria-label", () => {
    render(<PreliminaryBadge />);
    const badge = screen.getByTestId("preliminary-badge");
    expect(badge.textContent).toMatch(/preliminary/i);
    expect(badge.getAttribute("aria-label")).toMatch(/same-session/i);
  });
});

describe("ReviewStateBadge", () => {
  test.each([
    ["reviewed", /reviewed/i],
    ["low_confidence", /low confidence/i],
    ["needs_review", /needs review/i],
  ] as const)("renders %s label", (state, label) => {
    render(<ReviewStateBadge state={state} />);
    expect(screen.getByTestId(`review-state-${state}`).textContent).toMatch(label);
  });
});

describe("ClipStateBadges", () => {
  test("shows both pills for a preliminary, needs-review clip", () => {
    render(<ClipStateBadges isPreliminary reviewState="needs_review" />);
    expect(screen.getByTestId("preliminary-badge")).toBeTruthy();
    expect(screen.getByTestId("review-state-needs_review")).toBeTruthy();
  });

  test("omits the preliminary pill once a clip is final", () => {
    render(<ClipStateBadges isPreliminary={false} reviewState="reviewed" />);
    expect(screen.queryByTestId("preliminary-badge")).toBeNull();
    expect(screen.getByTestId("review-state-reviewed")).toBeTruthy();
  });

  test("renders nothing when no state is supplied", () => {
    const { container } = render(<ClipStateBadges />);
    expect(container.innerHTML).toBe("");
  });
});
