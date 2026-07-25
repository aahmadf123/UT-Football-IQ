// Corrections side panel (Hudl-lite labeling): happy-path POST contract,
// form clearing on success, and the viewer-role disabled state (the backend
// returns 403 for below-coach roles; the UI must disable and explain, not
// crash).
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { OverlayTracklet } from "@/lib/types";

afterEach(() => {
  cleanup();
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
  vi.resetModules();
});

beforeEach(() => {
  vi.stubEnv("NEXT_PUBLIC_USE_MOCKS", "");
  vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.test");
});

async function importPanel() {
  vi.resetModules();
  const [{ CorrectionsPanel }, { AppStateProvider }] = await Promise.all([
    import("./corrections-panel"),
    import("@/lib/app-state"),
  ]);
  return { CorrectionsPanel, AppStateProvider };
}

const TRACKLETS: OverlayTracklet[] = [
  {
    id: "t-1",
    player_id: null,
    start_frame: 0,
    end_frame: 300,
    track_confidence: 0.9,
    team_label: "home",
    position_group: "Skill",
    side_of_ball: "offense",
    track_points: [],
  },
  {
    id: "t-2",
    player_id: null,
    start_frame: 0,
    end_frame: 300,
    track_confidence: 0.8,
    team_label: "away",
    position_group: null,
    side_of_ball: "defense",
    track_points: [],
  },
];

/** Stub fetch: record POSTs to /corrections, return [] for app-state fetches. */
function installFetch(correctionResponse: { ok: boolean; status: number; body: unknown }) {
  const fetchMock = vi.fn(
    async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url.includes("/api/v1/corrections")) {
        return {
          ok: correctionResponse.ok,
          status: correctionResponse.status,
          json: async () => correctionResponse.body,
          text: async () => JSON.stringify(correctionResponse.body),
        } as unknown as Response;
      }
      void init;
      return {
        ok: true,
        status: 200,
        json: async () => [],
        text: async () => "[]",
      } as unknown as Response;
    },
  );
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

/** Unsigned-but-decodable JWT so roles.ts can read the role claim. */
function makeJwt(role: string): string {
  const b64 = (o: object) => Buffer.from(JSON.stringify(o)).toString("base64");
  return `${b64({ alg: "HS256", typ: "JWT" })}.${b64({ sub: "u-1", role, type: "access" })}.sig`;
}

describe("CorrectionsPanel", () => {
  test("player-identity happy path posts the correction contract and clears the form", async () => {
    const fetchMock = installFetch({
      ok: true,
      status: 201,
      body: {
        id: "cc-1",
        clip_id: "c-1",
        correction_type: "player_identity",
        original_value: null,
        corrected_value: {},
        corrected_by: "u-1",
        notes: null,
        training_eligible: true,
        exported_as_label: false,
        created_at: "2026-07-24T10:00:00Z",
      },
    });

    const { CorrectionsPanel, AppStateProvider } = await importPanel();
    render(
      <AppStateProvider>
        <CorrectionsPanel clipId="c-1" tracklets={TRACKLETS} />
      </AppStateProvider>,
    );

    // Collapsed by default; expand it.
    fireEvent.click(screen.getByTestId("corrections-toggle"));

    // Default kind is player identity; the submit stays disabled until a
    // tracklet + an actual fix are provided.
    const submitButton = screen.getByTestId("correction-submit") as HTMLButtonElement;
    expect(submitButton.disabled).toBe(true);

    fireEvent.change(screen.getByLabelText(/Player track/), {
      target: { value: "t-2" },
    });
    fireEvent.change(screen.getByLabelText(/Correct team/), {
      target: { value: "home" },
    });
    fireEvent.change(screen.getByLabelText(/Correct jersey #/), {
      target: { value: "22" },
    });
    fireEvent.change(screen.getByLabelText(/Note \(optional\)/), {
      target: { value: "that's our slot receiver" },
    });
    expect(submitButton.disabled).toBe(false);
    fireEvent.click(submitButton);

    await waitFor(() => {
      expect(screen.getByTestId("correction-success")).toBeTruthy();
    });
    expect(screen.getByTestId("correction-success").textContent).toContain(
      "feeds the nightly learning loop",
    );

    const call = fetchMock.mock.calls.find(([u]) =>
      String(u).includes("/api/v1/corrections"),
    );
    expect(call).toBeTruthy();
    const [url, init] = call as [string, RequestInit];
    expect(url).toBe("https://api.test/api/v1/corrections");
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({
      clip_id: "c-1",
      correction_type: "player_identity",
      corrected_value: { tracklet_id: "t-2", team: "home", jersey_number: 22 },
      original_value: { tracklet_id: "t-2", team: "away" },
      notes: "that's our slot receiver",
    });

    // Form cleared after success.
    expect((screen.getByLabelText(/Correct jersey #/) as HTMLInputElement).value).toBe("");
    expect((screen.getByLabelText(/Player track/) as HTMLSelectElement).value).toBe("");
  });

  test("formation correction posts free-text formation", async () => {
    const fetchMock = installFetch({ ok: true, status: 201, body: { id: "cc-2" } });

    const { CorrectionsPanel, AppStateProvider } = await importPanel();
    render(
      <AppStateProvider>
        <CorrectionsPanel clipId="c-1" tracklets={[]} />
      </AppStateProvider>,
    );

    fireEvent.click(screen.getByTestId("corrections-toggle"));
    fireEvent.change(screen.getByLabelText(/What needs fixing/), {
      target: { value: "formation_tag" },
    });
    fireEvent.change(screen.getByLabelText(/Correct formation/), {
      target: { value: "trips right" },
    });
    fireEvent.click(screen.getByTestId("correction-submit"));

    await waitFor(() => {
      expect(screen.getByTestId("correction-success")).toBeTruthy();
    });
    const call = fetchMock.mock.calls.find(([u]) =>
      String(u).includes("/api/v1/corrections"),
    );
    const [, init] = call as [string, RequestInit];
    expect(JSON.parse(String(init.body))).toEqual({
      clip_id: "c-1",
      correction_type: "formation_tag",
      corrected_value: { formation: "trips right" },
      notes: null,
    });
    expect((screen.getByLabelText(/Correct formation/) as HTMLInputElement).value).toBe("");
  });

  test("failed POST surfaces the error inline", async () => {
    installFetch({ ok: false, status: 500, body: { detail: "boom" } });

    const { CorrectionsPanel, AppStateProvider } = await importPanel();
    render(
      <AppStateProvider>
        <CorrectionsPanel clipId="c-1" tracklets={[]} />
      </AppStateProvider>,
    );

    fireEvent.click(screen.getByTestId("corrections-toggle"));
    fireEvent.change(screen.getByLabelText(/What needs fixing/), {
      target: { value: "event_tag" },
    });
    fireEvent.change(screen.getByLabelText(/What's wrong\?/), {
      target: { value: "boxes jump between players" },
    });
    fireEvent.click(screen.getByTestId("correction-submit"));

    await waitFor(() => {
      expect(screen.getByTestId("correction-error")).toBeTruthy();
    });
    expect(screen.getByTestId("correction-error").textContent).toContain("500");
    expect(screen.queryByTestId("correction-success")).toBeNull();
  });

  test("viewer role sees an explanation and disabled inputs instead of a 403", async () => {
    const fetchMock = installFetch({ ok: true, status: 201, body: { id: "cc-3" } });

    const { CorrectionsPanel, AppStateProvider } = await importPanel();
    render(
      <AppStateProvider authToken={makeJwt("viewer")}>
        <CorrectionsPanel clipId="c-1" tracklets={TRACKLETS} />
      </AppStateProvider>,
    );

    fireEvent.click(screen.getByTestId("corrections-toggle"));

    expect(screen.getByTestId("corrections-role-blocked").textContent).toContain(
      "Your role can't submit corrections",
    );
    expect((screen.getByTestId("correction-submit") as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByLabelText(/What needs fixing/) as HTMLSelectElement).disabled).toBe(true);
    expect((screen.getByLabelText(/Player track/) as HTMLSelectElement).disabled).toBe(true);
    expect((screen.getByLabelText(/Correct jersey #/) as HTMLInputElement).disabled).toBe(true);

    // No correction request went out.
    expect(
      fetchMock.mock.calls.some(([u]) => String(u).includes("/api/v1/corrections")),
    ).toBe(false);
  });
});
