import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

vi.mock("@/lib/api", () => ({ searchConcepts: vi.fn() }));
vi.mock("@/lib/app-state", () => ({ useAppState: vi.fn() }));

import { searchConcepts } from "@/lib/api";
import { useAppState } from "@/lib/app-state";
import type { ConceptSearchResponse } from "@/lib/types";
import { ConceptSearch } from "./concept-search";

const mockSearch = vi.mocked(searchConcepts);
const mockUseAppState = vi.mocked(useAppState);

function setAppState(partial: { authToken?: string; mockMode: boolean }) {
  mockUseAppState.mockReturnValue(partial as unknown as ReturnType<typeof useAppState>);
}

const RESPONSE: ConceptSearchResponse = {
  query: "cover 3 trips",
  matched_concepts: [
    { concept_id: "cover_3", display_name: "Cover 3", category: "coverage", confidence: 0.95 },
    { concept_id: "trips", display_name: "Trips (3x1)", category: "formation", confidence: 0.95 },
  ],
  approximate: true,
  experimental: true,
  reason: null,
  model_version_label: "play-embed-clip-vitb32-baseline@1.0",
  results: [
    {
      clip_id: "11111111-1111-1111-1111-111111111111",
      source: "metadata",
      confidence: 0.95,
      score: null,
      is_experimental: false,
      matched_concept_ids: ["cover_3", "trips"],
      label_data: { coverage: { generic: "cover_3" }, formation: { generic: "trips_right" } },
    },
    {
      clip_id: "22222222-2222-2222-2222-222222222222",
      source: "embedding",
      confidence: 0.81,
      score: 0.81,
      is_experimental: true,
      matched_concept_ids: ["cover_3", "trips"],
      label_data: null,
    },
  ],
};

beforeEach(() => {
  mockSearch.mockReset();
  mockUseAppState.mockReset();
});

afterEach(() => cleanup());

describe("ConceptSearch", () => {
  test("renders an Approximate badge on the search box", () => {
    setAppState({ authToken: "tok", mockMode: false });
    render(<ConceptSearch />);
    expect(screen.getByText("Approximate")).toBeTruthy();
    expect(screen.getByLabelText("Approximate metric — not a validated result")).toBeTruthy();
  });

  test("searches the backend and labels experimental embedding results", async () => {
    setAppState({ authToken: "tok", mockMode: false });
    mockSearch.mockResolvedValue(RESPONSE);
    render(<ConceptSearch />);

    fireEvent.change(screen.getByLabelText("Concept search query"), {
      target: { value: "cover 3 trips" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));

    const results = await screen.findByTestId("concept-search-results");
    expect(results).toBeTruthy();
    expect(mockSearch).toHaveBeenCalledWith("cover 3 trips", { k: 20 }, "tok");
    // Matched concept chips surface.
    expect(screen.getByText("Cover 3")).toBeTruthy();
    // "Approximate" header badge + one EXPERIMENTAL badge for the embedding row.
    expect(screen.getAllByTestId("experimental-badge").length).toBeGreaterThanOrEqual(2);
    expect(screen.getAllByText(/experimental/i).length).toBeGreaterThanOrEqual(1);
  });

  test("does not call the backend or fabricate results in mock mode", async () => {
    setAppState({ authToken: undefined, mockMode: true });
    render(<ConceptSearch />);

    fireEvent.change(screen.getByLabelText("Concept search query"), {
      target: { value: "mesh" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));

    expect(mockSearch).not.toHaveBeenCalled();
    expect(screen.getByText(/mock mode/i)).toBeTruthy();
    expect(screen.queryByTestId("concept-search-results")).toBeNull();
  });
});
