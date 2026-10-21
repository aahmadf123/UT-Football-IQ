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
  test.each(["pass_target", "pass_result", "reception", "possession_end", "carrier_cut", "tackle_outcome"])("%s saves the explicit reviewed possession contract", async (type) => {
    const fetchMock = installFetch({ ok: true, status: 201, body: { id: "reviewed-outcome" } });
    const { CorrectionsPanel, AppStateProvider } = await importPanel();
    const tracks = TRACKLETS.map((track) => ({ ...track, track_points: [{ frame_number: 0, timestamp_seconds: 0, shot_id: "native-shot", bbox: [1, 2, 3, 4] as [number, number, number, number], field_x: null, field_y: null, detection_confidence: 1 }] }));
    render(<AppStateProvider><CorrectionsPanel clipId="c-1" tracklets={tracks} currentTimeSeconds={0.2} /></AppStateProvider>);
    fireEvent.click(screen.getByTestId("corrections-toggle"));
    fireEvent.change(screen.getByLabelText(/What needs fixing/), { target: { value: "position_observation" } });
    fireEvent.change(screen.getByLabelText("Observation"), { target: { value: type } });
    fireEvent.change(screen.getByLabelText("Observed player track"), { target: { value: "t-1" } });
    fireEvent.change(screen.getByLabelText("Possession name"), { target: { value: "drive-1-pass-2" } });
    if (type === "pass_result" || type === "possession_end" || type === "tackle_outcome") {
      fireEvent.change(screen.getByLabelText("Reviewed outcome"), { target: { value: type === "pass_result" ? "complete" : type === "possession_end" ? "out_of_bounds" : "missed" } });
    }
    for (const label of ["I observed an aimed forward-pass target for this player", "I observed this offensive player complete a forward-pass reception", "I observed a cut by the player in possession", "I reviewed this defender's tackle attempt against the selected carrier"]) {
      const checkbox = screen.queryByLabelText(label);
      if (checkbox) fireEvent.click(checkbox);
    }
    if (type === "pass_target" || type === "tackle_outcome") {
      fireEvent.change(screen.getByLabelText("Comparison player (optional)"), { target: { value: "t-2" } });
    }
    if (type === "pass_target") fireEvent.click(screen.getByLabelText("The selected defender had reviewed responsibility for this target"));
    if (type === "pass_result") fireEvent.change(screen.getByLabelText("Reviewed contest"), { target: { value: "yes" } });
    if (type === "tackle_outcome") fireEvent.click(screen.getByLabelText("Film review attributes the missed tackle to the carrier"));
    fireEvent.click(screen.getByTestId("correction-submit"));
    await waitFor(() => expect(screen.getByTestId("correction-success")).toBeTruthy());
    const call = fetchMock.mock.calls.find(([url]) => String(url).includes("/api/v1/corrections"));
    const value = JSON.parse(String(call?.[1]?.body)).corrected_value;
    expect(value).toMatchObject({ event_type: type, timestamp_seconds: 0.2, timebase: "clip", attributes: { actor_id: "t-1", shot_id: "native-shot", possession_id: "drive-1-pass-2" } });
    const expected: Record<string, object> = { pass_target: { forward_pass: true, opponent_id: "t-2", responsibility_confirmed: true }, pass_result: { outcome: "complete", contested: true }, reception: { forward_pass: true, completion_observed: true, possession_side: "offense" }, possession_end: { possession_ended: true, terminal_reason: "out_of_bounds" }, carrier_cut: { cut_observed: true }, tackle_outcome: { opponent_id: "t-2", outcome: "missed", tackle_observed: true, forced_by_carrier: true } };
    expect(value.attributes).toMatchObject(expected[type]);
  });
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
      "Saved to this play",
    );

    const call = fetchMock.mock.calls.find(([u]) =>
      String(u).includes("/api/v1/corrections"),
    );
    expect(call).toBeTruthy();
    const [url, init] = call as [string, RequestInit];
    expect(url).toBe("https://api.test/api/v1/corrections");
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toMatchObject({
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
    expect(JSON.parse(String(init.body))).toMatchObject({
      clip_id: "c-1",
      correction_type: "formation_tag",
      corrected_value: { formation: "trips right" },
      notes: null,
    });
    expect((screen.getByLabelText(/Correct formation/) as HTMLInputElement).value).toBe("");
  });

  test.each([
    { kind: "coverage_tag", label: "Correct coverage", value: "cover_3", key: "coverage" },
    { kind: "route_tag", label: "Correct route", value: "curl", key: "route" },
    { kind: "personnel_tag", label: "Correct personnel", value: "11", key: "personnel" },
  ])("reviewed human $kind values are sent with revision and refresh the play", async ({ kind, label, value, key }) => {
    const fetchMock = installFetch({ ok: true, status: 201, body: { id: "cc-tag", annotation_revision: 5 } });
    const onSaved = vi.fn();
    const { CorrectionsPanel, AppStateProvider } = await importPanel();
    render(<AppStateProvider><CorrectionsPanel clipId="c-1" tracklets={[]} annotationRevision={4} onSaved={onSaved} /></AppStateProvider>);
    fireEvent.click(screen.getByTestId("corrections-toggle"));
    fireEvent.change(screen.getByLabelText(/What needs fixing/), { target: { value: kind } });
    fireEvent.change(screen.getByLabelText(label), { target: { value } });
    fireEvent.click(screen.getByTestId("correction-submit"));
    await waitFor(() => expect(onSaved).toHaveBeenCalledOnce());
    const call = fetchMock.mock.calls.find(([url]) => String(url).includes("/api/v1/corrections"));
    const body = JSON.parse(String(call?.[1]?.body));
    expect(body).toMatchObject({ correction_type: kind, corrected_value: { [key]: value }, expected_revision: 4 });
    expect(body.client_operation_id).toMatch(/^[a-f0-9-]{36}$/);
    expect((screen.getByLabelText(label) as HTMLInputElement).value).toBe("");
  });

  test("a network retry reuses the correction operation id and preserves the coach input", async () => {
    const response = { ok: false, status: 503, body: { detail: "temporarily unavailable" } };
    const fetchMock = installFetch(response);
    const { CorrectionsPanel, AppStateProvider } = await importPanel();
    render(<AppStateProvider><CorrectionsPanel clipId="c-1" tracklets={[]} annotationRevision={4} /></AppStateProvider>);
    fireEvent.click(screen.getByTestId("corrections-toggle"));
    fireEvent.change(screen.getByLabelText(/What needs fixing/), { target: { value: "coverage_tag" } });
    fireEvent.change(screen.getByLabelText("Correct coverage"), { target: { value: "cover_3" } });
    fireEvent.click(screen.getByTestId("correction-submit"));
    await waitFor(() => expect(screen.getByTestId("correction-error")).toBeTruthy());
    expect((screen.getByLabelText("Correct coverage") as HTMLSelectElement).value).toBe("cover_3");
    response.ok = true;
    response.status = 201;
    fireEvent.click(screen.getByTestId("correction-submit"));
    await waitFor(() => expect(screen.getByTestId("correction-success")).toBeTruthy());
    const posts = fetchMock.mock.calls.filter(([url]) => String(url).includes("/api/v1/corrections"));
    expect(posts).toHaveLength(2);
    const bodies = posts.map(([, init]) => JSON.parse(String(init?.body)));
    expect(bodies[1].client_operation_id).toBe(bodies[0].client_operation_id);
    expect(bodies[1].corrected_value).toEqual({ coverage: "cover_3" });
  });

  test("manual play event uses the displayed clip time rather than the recording clock", async () => {
    const fetchMock = installFetch({ ok: true, status: 201, body: { id: "cc-event" } });
    const { CorrectionsPanel, AppStateProvider } = await importPanel();
    render(<AppStateProvider><CorrectionsPanel clipId="c-1" tracklets={[]} clipBounds={{ start: 120, end: 128, duration: 900 }} currentTimeSeconds={2.5} /></AppStateProvider>);
    fireEvent.click(screen.getByTestId("corrections-toggle"));
    fireEvent.change(screen.getByLabelText(/What needs fixing/), { target: { value: "event_mark" } });
    fireEvent.click(screen.getByTestId("correction-submit"));
    await waitFor(() => expect(screen.getByTestId("correction-success")).toBeTruthy());
    const call = fetchMock.mock.calls.find(([url]) => String(url).includes("/api/v1/corrections"));
    expect(JSON.parse(String(call?.[1]?.body))).toMatchObject({ correction_type: "event_tag", corrected_value: { event_type: "snap", timestamp_seconds: 2.5 } });
  });

  test("manual play event uses the live playhead when submitted", async () => {
    const fetchMock = installFetch({ ok: true, status: 201, body: { id: "cc-event" } });
    const { CorrectionsPanel, AppStateProvider } = await importPanel();
    render(<AppStateProvider><CorrectionsPanel clipId="c-1" tracklets={[]} currentTimeSeconds={2.5} getCurrentTimeSeconds={() => 4.75} /></AppStateProvider>);
    fireEvent.click(screen.getByTestId("corrections-toggle"));
    fireEvent.change(screen.getByLabelText(/What needs fixing/), { target: { value: "event_mark" } });
    fireEvent.click(screen.getByTestId("correction-submit"));
    await waitFor(() => expect(screen.getByTestId("correction-success")).toBeTruthy());
    const call = fetchMock.mock.calls.find(([url]) => String(url).includes("/api/v1/corrections"));
    expect(JSON.parse(String(call?.[1]?.body))).toMatchObject({ corrected_value: { timestamp_seconds: 4.75 } });
  });

  test("position participation records an explicit actor role side shot and play interval", async () => {
    const fetchMock = installFetch({ ok: true, status: 201, body: { id: "position-event" } });
    const { CorrectionsPanel, AppStateProvider } = await importPanel();
    const tracks = TRACKLETS.map((track) => ({ ...track, track_points: [{ frame_number: 1200, timestamp_seconds: 120, shot_id: "native-shot", bbox: [1, 2, 3, 4] as [number, number, number, number], field_x: null, field_y: null, detection_confidence: 1 }] }));
    render(<AppStateProvider><CorrectionsPanel clipId="c-1" tracklets={tracks} clipBounds={{ start: 120, end: 128, duration: 900 }} currentTimeSeconds={1} /></AppStateProvider>);
    fireEvent.click(screen.getByTestId("corrections-toggle"));
    fireEvent.change(screen.getByLabelText(/What needs fixing/), { target: { value: "position_observation" } });
    fireEvent.change(screen.getByLabelText("Observed player track"), { target: { value: "t-1" } });
    fireEvent.change(screen.getByLabelText("Side of the ball"), { target: { value: "offense" } });
    fireEvent.change(screen.getByLabelText("Observed role"), { target: { value: "WR" } });
    fireEvent.change(screen.getByLabelText("Opportunity"), { target: { value: "routes" } });
    fireEvent.change(screen.getByLabelText("Participation end (play seconds)"), { target: { value: "5" } });
    fireEvent.change(screen.getByLabelText("Attacking direction"), { target: { value: "1" } });
    fireEvent.change(screen.getByLabelText("Comparison player (optional)"), { target: { value: "t-2" } });
    fireEvent.click(screen.getByTestId("correction-submit"));
    await waitFor(() => expect(screen.getByTestId("correction-success")).toBeTruthy());
    const call = fetchMock.mock.calls.find(([url]) => String(url).includes("/api/v1/corrections"));
    expect(JSON.parse(String(call?.[1]?.body))).toMatchObject({ correction_type: "event_tag", corrected_value: {
      event_type: "position_participation", timestamp_seconds: 1, timebase: "clip",
      attributes: { actor_id: "t-1", observed_role: "WR", team_label: "offense", opportunity_kind: "routes", end_seconds: 5, interval_timebase: "clip", attacking_direction: 1, opponent_id: "t-2", shot_id: "native-shot" },
    } });
  });

  test("position observations cannot save an unknown shot or invented actor", async () => {
    const fetchMock = installFetch({ ok: true, status: 201, body: { id: "position-event" } });
    const { CorrectionsPanel, AppStateProvider } = await importPanel();
    render(<AppStateProvider><CorrectionsPanel clipId="c-1" tracklets={TRACKLETS} currentTimeSeconds={1} /></AppStateProvider>);
    fireEvent.click(screen.getByTestId("corrections-toggle"));
    fireEvent.change(screen.getByLabelText(/What needs fixing/), { target: { value: "position_observation" } });
    expect((screen.getByTestId("correction-submit") as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText(/No source shot is available/)).toBeTruthy();
    expect(fetchMock.mock.calls.filter(([url]) => String(url).includes("/api/v1/corrections"))).toHaveLength(0);
  });

  test.each(["te_usage", "punt", "landing"])("linked %s observations carry the interaction or operation evidence", async (type) => {
    const fetchMock = installFetch({ ok: true, status: 201, body: { id: "position-event" } });
    const { CorrectionsPanel, AppStateProvider } = await importPanel();
    const tracks = TRACKLETS.map((track) => ({ ...track, track_points: [{ frame_number: 0, timestamp_seconds: 0, shot_id: "shot", bbox: [1, 2, 3, 4] as [number, number, number, number], field_x: null, field_y: null, detection_confidence: 1 }] }));
    render(<AppStateProvider><CorrectionsPanel clipId="c-1" tracklets={tracks} currentTimeSeconds={1} /></AppStateProvider>);
    fireEvent.click(screen.getByTestId("corrections-toggle"));
    fireEvent.change(screen.getByLabelText(/What needs fixing/), { target: { value: "position_observation" } });
    fireEvent.change(screen.getByLabelText("Observation"), { target: { value: type } });
    fireEvent.change(screen.getByLabelText("Observed player track"), { target: { value: "t-1" } });
    if (type === "te_usage") {
      fireEvent.change(screen.getByLabelText("Comparison player (optional)"), { target: { value: "t-2" } });
      fireEvent.change(screen.getByLabelText("Interaction name"), { target: { value: "chip-1" } });
    } else {
      fireEvent.change(screen.getByLabelText("Kicking operation name"), { target: { value: "punt-1" } });
      fireEvent.change(screen.getByLabelText("Observed ball track"), { target: { value: "t-2" } });
      if (type === "landing") {
        fireEvent.change(screen.getByLabelText("Measured landing field x (yards)"), { target: { value: "95" } });
        fireEvent.change(screen.getByLabelText("Measured landing field y (yards)"), { target: { value: "-4" } });
      }
    }
    fireEvent.click(screen.getByTestId("correction-submit"));
    await waitFor(() => expect(screen.getByTestId("correction-success")).toBeTruthy());
    const call = fetchMock.mock.calls.find(([url]) => String(url).includes("/api/v1/corrections"));
    const value = JSON.parse(String(call?.[1]?.body)).corrected_value;
    expect(value).toMatchObject({ event_type: type, timestamp_seconds: 1, attributes: { actor_id: "t-1", shot_id: "shot" } });
    expect(value.attributes).toMatchObject(type === "te_usage" ? { state: "chip", interaction_id: "chip-1", opponent_id: "t-2" } : { operation_id: "punt-1", ball_id: "t-2" });
    if (type === "landing") expect(value.attributes).toMatchObject({ field_x: 95, field_y: -4 });
  });

  test("measured envelope vertices and a declared native window use the structured writer", async () => {
    const fetchMock=installFetch({ok:true,status:201,body:{id:"polygon"}});
    const {CorrectionsPanel,AppStateProvider}=await importPanel();
    const tracks=TRACKLETS.map(track=>({...track,track_points:[{frame_number:0,timestamp_seconds:0,shot_id:"native-shot",bbox:[1,2,3,4] as [number,number,number,number],field_x:null,field_y:null,detection_confidence:1}]}));
    render(<AppStateProvider><CorrectionsPanel clipId="c-1" tracklets={tracks} currentTimeSeconds={0.1} /></AppStateProvider>);
    fireEvent.click(screen.getByTestId("corrections-toggle"));
    fireEvent.change(screen.getByLabelText(/What needs fixing/),{target:{value:"position_observation"}});
    fireEvent.change(screen.getByLabelText("Observation"),{target:{value:"protection_envelope"}});
    fireEvent.change(screen.getByLabelText("Envelope name"),{target:{value:"observed-pocket"}});
    fireEvent.change(screen.getByLabelText("Measurement window start (play seconds)"),{target:{value:"0"}});
    fireEvent.change(screen.getByLabelText("Measurement window end (play seconds)"),{target:{value:"0.2"}});
    for(const [index,x,y] of [[1,45,-2],[2,48,-2],[3,45,2]]) {
      fireEvent.change(screen.getByLabelText(`Envelope vertex ${index} field x (yards)`),{target:{value:String(x)}});
      fireEvent.change(screen.getByLabelText(`Envelope vertex ${index} field y (yards)`),{target:{value:String(y)}});
    }
    fireEvent.click(screen.getByTestId("correction-submit"));
    await waitFor(()=>expect(screen.getByTestId("correction-success")).toBeTruthy());
    const call=fetchMock.mock.calls.find(([url])=>String(url).includes("/api/v1/corrections"));
    expect(JSON.parse(String(call?.[1]?.body))).toMatchObject({corrected_value:{event_type:"protection_envelope",timestamp_seconds:0.1,
      attributes:{envelope_id:"observed-pocket",start_seconds:0,end_seconds:0.2,interval_timebase:"clip",visibility_state:"observed",
        polygon:[{field_x:45,field_y:-2},{field_x:48,field_y:-2},{field_x:45,field_y:2}]}}});
  });

  test.each(["unknown","unverified","none","null"])("placeholder shot %s cannot author specialist observations",async(shot)=>{
    installFetch({ok:true,status:201,body:{id:"no"}});
    const {CorrectionsPanel,AppStateProvider}=await importPanel();
    const tracks=TRACKLETS.map(track=>({...track,track_points:[{frame_number:0,timestamp_seconds:0,shot_id:shot,bbox:[1,2,3,4] as [number,number,number,number],field_x:null,field_y:null,detection_confidence:1}]}));
    render(<AppStateProvider><CorrectionsPanel clipId="c-1" tracklets={tracks} /></AppStateProvider>);
    fireEvent.click(screen.getByTestId("corrections-toggle"));
    fireEvent.change(screen.getByLabelText(/What needs fixing/),{target:{value:"position_observation"}});
    expect(screen.getByText(/No source shot is available/)).toBeTruthy();
  });

  test.each([["RB","offense","rushes"],["WR","offense","targets"],["LT","offense","pass_protection"],["LB","defense","tackle_opportunities"],["KR","special_teams","returns"]])("independent %s %s %s participation needs no outcome/end",async(role,side,opportunity)=>{
    const fetchMock=installFetch({ok:true,status:201,body:{id:"opportunity"}});
    const {CorrectionsPanel,AppStateProvider}=await importPanel();
    const tracks=TRACKLETS.map(track=>({...track,track_points:[{frame_number:0,timestamp_seconds:0,shot_id:"native-shot",bbox:[1,2,3,4] as [number,number,number,number],field_x:null,field_y:null,detection_confidence:1}]}));
    render(<AppStateProvider><CorrectionsPanel clipId="c-1" tracklets={tracks} currentTimeSeconds={1} /></AppStateProvider>);
    fireEvent.click(screen.getByTestId("corrections-toggle"));
    fireEvent.change(screen.getByLabelText(/What needs fixing/),{target:{value:"position_observation"}});
    fireEvent.change(screen.getByLabelText("Observed player track"),{target:{value:"t-1"}});
    fireEvent.change(screen.getByLabelText("Side of the ball"),{target:{value:side}});
    fireEvent.change(screen.getByLabelText("Observed role"),{target:{value:role}});
    fireEvent.change(screen.getByLabelText("Opportunity"),{target:{value:opportunity}});
    fireEvent.click(screen.getByTestId("correction-submit"));
    await waitFor(()=>expect(screen.getByTestId("correction-success")).toBeTruthy());
    const call=fetchMock.mock.calls.find(([url])=>String(url).includes("/api/v1/corrections"));
    const attrs=JSON.parse(String(call?.[1]?.body)).corrected_value.attributes;
    expect(attrs).toMatchObject({actor_id:"t-1",observed_role:role,team_label:side,opportunity_kind:opportunity});
    expect(attrs).not.toHaveProperty("end_seconds");
  });

  test("pressure interval records explicit QB state and reviewed completeness",async()=>{
    const fetchMock=installFetch({ok:true,status:201,body:{id:"pressure"}});
    const {CorrectionsPanel,AppStateProvider}=await importPanel();
    const tracks=TRACKLETS.map(track=>({...track,track_points:[{frame_number:0,timestamp_seconds:0,shot_id:"native-shot",bbox:[1,2,3,4] as [number,number,number,number],field_x:null,field_y:null,detection_confidence:1}]}));
    render(<AppStateProvider><CorrectionsPanel clipId="c-1" tracklets={tracks} currentTimeSeconds={0.2} /></AppStateProvider>);
    fireEvent.click(screen.getByTestId("corrections-toggle"));
    fireEvent.change(screen.getByLabelText(/What needs fixing/),{target:{value:"position_observation"}});
    fireEvent.change(screen.getByLabelText("Observation"),{target:{value:"pressure_observation"}});
    fireEvent.change(screen.getByLabelText("Observed player track"),{target:{value:"t-1"}});
    fireEvent.change(screen.getByLabelText("Observed pressure state"),{target:{value:"none"}});
    fireEvent.change(screen.getByLabelText("Pressure interval end (play seconds)"),{target:{value:"1"}});
    fireEvent.click(screen.getByLabelText("Pressure state was observed continuously for this entire interval"));
    fireEvent.click(screen.getByTestId("correction-submit"));
    await waitFor(()=>expect(screen.getByTestId("correction-success")).toBeTruthy());
    const call=fetchMock.mock.calls.find(([url])=>String(url).includes("/api/v1/corrections"));
    expect(JSON.parse(String(call?.[1]?.body)).corrected_value).toMatchObject({event_type:"pressure_observation",timestamp_seconds:0.2,
      attributes:{qb_id:"t-1",pressure_state:"none",start_seconds:0.2,end_seconds:1,interval_timebase:"clip",coverage_complete:true}});
  });

  test.each(["engagement_start","engagement_end","chip_end","route_start"])("%s writes exact paired producer attributes",async(type)=>{
    const fetchMock=installFetch({ok:true,status:201,body:{id:"interaction"}});
    const {CorrectionsPanel,AppStateProvider}=await importPanel();
    const tracks=TRACKLETS.map(track=>({...track,track_points:[{frame_number:0,timestamp_seconds:0,shot_id:"native-shot",bbox:[1,2,3,4] as [number,number,number,number],field_x:null,field_y:null,detection_confidence:1}]}));
    render(<AppStateProvider><CorrectionsPanel clipId="c-1" tracklets={tracks} currentTimeSeconds={0.2} /></AppStateProvider>);
    fireEvent.click(screen.getByTestId("corrections-toggle"));
    fireEvent.change(screen.getByLabelText(/What needs fixing/),{target:{value:"position_observation"}});
    fireEvent.change(screen.getByLabelText("Observation"),{target:{value:type}});
    fireEvent.change(screen.getByLabelText("Observed player track"),{target:{value:"t-1"}});
    fireEvent.change(screen.getByLabelText("Comparison player (optional)"),{target:{value:"t-2"}});
    fireEvent.change(screen.getByLabelText("Interaction name"),{target:{value:"observed-pair"}});
    fireEvent.click(screen.getByTestId("correction-submit"));
    await waitFor(()=>expect(screen.getByTestId("correction-success")).toBeTruthy());
    const call=fetchMock.mock.calls.find(([url])=>String(url).includes("/api/v1/corrections"));
    expect(JSON.parse(String(call?.[1]?.body)).corrected_value.attributes).toMatchObject({blocker_id:"t-1",defender_id:"t-2",interaction_id:"observed-pair"});
  });

  test("measured body and explicit occupancy fields reach the source-shot snapshot API",async()=>{
    const fetchMock=installFetch({ok:true,status:201,body:{id:"geometry"}});
    const {CorrectionsPanel,AppStateProvider}=await importPanel();
    const tracks=TRACKLETS.map(track=>({...track,track_points:[{frame_number:0,timestamp_seconds:0,shot_id:"1",bbox:[1,2,3,4] as [number,number,number,number],field_x:null,field_y:null,detection_confidence:1}]}));
    render(<AppStateProvider><CorrectionsPanel clipId="c-1" tracklets={tracks} currentTimeSeconds={0.2} /></AppStateProvider>);
    fireEvent.click(screen.getByTestId("corrections-toggle"));
    fireEvent.change(screen.getByLabelText(/What needs fixing/),{target:{value:"position_observation"}});
    fireEvent.change(screen.getByLabelText("Observation"),{target:{value:"geometry_observation"}});
    fireEvent.change(screen.getByLabelText("Body 1 observed track"),{target:{value:"t-2"}});
    for(const [key,value] of [["min x","49"],["max x","51"],["min y","-1"],["max y","1"]]) fireEvent.change(screen.getByLabelText(`Body 1 measured ${key} (yards)`),{target:{value}});
    fireEvent.click(screen.getByLabelText("Defender T2"));
    fireEvent.click(screen.getByLabelText("The complete observed defender population is listed with measured footprints"));
    fireEvent.click(screen.getByTestId("correction-submit"));
    await waitFor(()=>expect(screen.getByTestId("correction-success")).toBeTruthy());
    const call=fetchMock.mock.calls.find(([url])=>String(url).includes("/api/v1/corrections"));
    expect(JSON.parse(String(call?.[1]?.body)).corrected_value).toMatchObject({event_type:"geometry_observation",attributes:{shot_id:"1",body_extents:{"t-2":{min_x:49,max_x:51,min_y:-1,max_y:1}},defender_occupancy:{complete:true,track_ids:["t-2"]}}});
  });

  test("qualified snap writes measured LOS direction and original named-role map",async()=>{
    const fetchMock=installFetch({ok:true,status:201,body:{id:"snap"}});
    const {CorrectionsPanel,AppStateProvider}=await importPanel();
    const tracks=Array.from({length:6},(_,index)=>({...TRACKLETS[0],id:`t-${index+1}`,track_points:[{frame_number:0,timestamp_seconds:0,shot_id:"0",bbox:[1,2,3,4] as [number,number,number,number],field_x:null,field_y:null,detection_confidence:1}]}));
    render(<AppStateProvider><CorrectionsPanel clipId="c-1" tracklets={tracks} currentTimeSeconds={0} /></AppStateProvider>);
    fireEvent.click(screen.getByTestId("corrections-toggle"));
    fireEvent.change(screen.getByLabelText(/What needs fixing/),{target:{value:"position_observation"}});
    fireEvent.change(screen.getByLabelText("Observation"),{target:{value:"snap"}});
    fireEvent.change(screen.getByLabelText("Observed player track"),{target:{value:"t-6"}});
    fireEvent.change(screen.getByLabelText("Measured LOS field x (yards)"),{target:{value:"50"}});
    fireEvent.change(screen.getByLabelText("Attacking direction"),{target:{value:"-1"}});
    for(const [index,role] of ["LT","LG","C","RG","RT"].entries()) fireEvent.change(screen.getByLabelText(`Original ${role} track`),{target:{value:`t-${index+1}`}});
    fireEvent.change(screen.getByLabelText("Original QB track (optional)"),{target:{value:"t-6"}});
    fireEvent.click(screen.getByTestId("correction-submit"));
    await waitFor(()=>expect(screen.getByTestId("correction-success")).toBeTruthy());
    const call=fetchMock.mock.calls.find(([url])=>String(url).includes("/api/v1/corrections"));
    expect(JSON.parse(String(call?.[1]?.body)).corrected_value.attributes).toMatchObject({shot_id:"0",los_x:50,attacking_direction:-1,qb_id:"t-6",center_id:"t-3",ol_roles:{LT:"t-1",LG:"t-2",C:"t-3",RG:"t-4",RT:"t-5"}});
  });

  test.each(["handoff","first_contact","end_of_play"])("%s links the explicit carrier without a guessed spot",async(type)=>{
    const fetchMock=installFetch({ok:true,status:201,body:{id:"carrier"}});
    const {CorrectionsPanel,AppStateProvider}=await importPanel();
    const tracks=TRACKLETS.map(track=>({...track,track_points:[{frame_number:0,timestamp_seconds:0,shot_id:"1",bbox:[1,2,3,4] as [number,number,number,number],field_x:null,field_y:null,detection_confidence:1}]}));
    render(<AppStateProvider><CorrectionsPanel clipId="c-1" tracklets={tracks} currentTimeSeconds={0.2} /></AppStateProvider>);
    fireEvent.click(screen.getByTestId("corrections-toggle"));
    fireEvent.change(screen.getByLabelText(/What needs fixing/),{target:{value:"position_observation"}});
    fireEvent.change(screen.getByLabelText("Observation"),{target:{value:type}});
    fireEvent.change(screen.getByLabelText("Observed player track"),{target:{value:"t-1"}});
    fireEvent.click(screen.getByTestId("correction-submit"));
    await waitFor(()=>expect(screen.getByTestId("correction-success")).toBeTruthy());
    const call=fetchMock.mock.calls.find(([url])=>String(url).includes("/api/v1/corrections"));
    const attrs=JSON.parse(String(call?.[1]?.body)).corrected_value.attributes;
    expect(attrs).toMatchObject({carrier_id:"t-1",shot_id:"1"});
    expect(attrs).not.toHaveProperty("field_x");
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
    expect(screen.getByTestId("correction-error").textContent).toContain("Something went wrong on our side");
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
