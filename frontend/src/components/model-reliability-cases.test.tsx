import { afterEach, expect, test, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import producer from "./__fixtures__/model-reliability-cases-producer.json";
import type { LearningEvaluation } from "@/lib/types";

const api = vi.hoisted(() => ({ load:vi.fn() }));
vi.mock("@/lib/model-reliability-cases", async original => ({ ...await original<typeof import("@/lib/model-reliability-cases")>(), loadHeldoutCases:api.load }));
import { ModelReliabilityCases } from "./model-reliability-cases";

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); api.load.mockReset(); window.history.replaceState(null,"","/"); });
const report = producer.evaluation as LearningEvaluation;
function mount() { api.load.mockResolvedValue(producer.page); return render(<ModelReliabilityCases report={report} authToken="staff-token" />); }

test("actual stored producer cases show descriptive baseline/source/category diagnostics", async () => {
  expect(producer.fixture_notice).toContain("not real football validation");
  mount();
  await screen.findByText(/20 of 20 held-out plays/);
  expect(api.load).toHaveBeenCalledWith(report.id,{dimension:"",category:"",offset:0},"staff-token");
  expect(screen.getByText(/Filters do not inherit release approval/)).toBeTruthy();
  expect(screen.getByText(/reference stays the frozen training baseline/)).toBeTruthy();
  expect(screen.getByRole("table",{name:"Selected category calibration"})).toBeTruthy();
  expect(screen.getByRole("table",{name:"Selected category confusion: observed rows, predicted columns"})).toBeTruthy();
  expect(screen.getAllByText(/No exact current trusted export matches/,{selector:"p"})).toHaveLength(20);
  expect(screen.queryByRole("link")).toBeNull();
});

test("category requests reject stale all-case responses and show the frozen baseline on small slices", async () => {
  let oldResolve: ((value:unknown) => void) | undefined;
  api.load.mockImplementation((_id,filter) => filter.dimension && filter.category === "1" ? Promise.resolve(producer.selected) : new Promise(resolve => { oldResolve = resolve; }));
  render(<ModelReliabilityCases report={report} />);
  fireEvent.change(screen.getByLabelText("Held-out case dimension"),{target:{value:"observed_outcome"}});
  fireEvent.change(screen.getByLabelText("Held-out case category"),{target:{value:"1"}});
  await screen.findByText(/10 of 20 held-out plays/);
  expect(screen.getByText(/Small descriptive slice/)).toBeTruthy();
  await act(async () => { oldResolve?.(producer.page); });
  expect(screen.queryByText(/20 of 20 held-out plays/)).toBeNull();
  expect(screen.queryByRole("alert")).toBeNull();
  expect(window.location.search).toContain("reliability_case_category=1");
});

test("download includes all selected cases and identities without claiming current film state", async () => {
  let blob: Blob | undefined;
  vi.stubGlobal("URL",class extends URL { static createObjectURL = vi.fn((value:Blob) => {blob=value;return "blob:cases";}); static revokeObjectURL=vi.fn(); });
  const click=vi.spyOn(HTMLAnchorElement.prototype,"click").mockImplementation(() => {});
  api.load.mockResolvedValue(producer.page);
  render(<ModelReliabilityCases report={report} authToken="staff-token" />);
  await screen.findByText(/20 of 20 held-out plays/);
  fireEvent.click(screen.getByRole("button",{name:"Download selected cases"}));
  await waitFor(() => expect(click).toHaveBeenCalledOnce());
  expect(api.load).toHaveBeenLastCalledWith(report.id,{dimension:"",category:"",offset:0,download:true},"staff-token");
  const body=await new Promise<string>(resolve => {const reader=new FileReader();reader.onload=()=>resolve(String(reader.result));reader.readAsText(blob!);});
  expect(JSON.parse(body).case_sha256).toBe(producer.page.case_sha256);
  expect(JSON.parse(body).cases).toHaveLength(20);
  expect((click.mock.instances[0] as unknown as HTMLAnchorElement).download).toBe(`heldout-cases-${report.id}.json`);
});

test("film href returns to the actual category and cohort URL, then rehydrates that selection", async () => {
  window.history.replaceState(null,"","/team-stats/?tab=analyst&session_ids=reviewed-session");
  const selected=structuredClone(producer.selected) as unknown as Record<string,unknown>;
  const cases=selected.cases as Record<string,unknown>[];
  cases[0].film={clip_id:"exact-clip",focus_seconds:1.25,source_export_job_id:"actual-export-job",source_export_sha256:"a".repeat(64)};
  cases[0].film_unavailable_reason=null;
  api.load.mockImplementation((_id,filter) => Promise.resolve(filter.category === "1" ? selected : producer.page));
  const view=render(<ModelReliabilityCases report={report} />);
  await screen.findByText(/20 of 20 held-out plays/);
  fireEvent.change(screen.getByLabelText("Held-out case dimension"),{target:{value:"observed_outcome"}});
  fireEvent.change(screen.getByLabelText("Held-out case category"),{target:{value:"1"}});
  const link=await screen.findByRole("link",{name:"Watch exact source evidence"});
  const destination=new URL(link.getAttribute("href")!,"https://fixture.invalid");
  expect(destination.pathname.replace(/\/$/,"")).toBe("/clip-review");
  expect(destination.searchParams.get("t")).toBe("1.25");
  const back=destination.searchParams.get("from")!;
  expect(back).toContain("/team-stats/?tab=analyst&session_ids=reviewed-session");
  expect(new URL(back,"https://fixture.invalid").searchParams.get("reliability_case_category")).toBe("1");
  view.unmount(); window.history.replaceState(null,"",back);
  render(<ModelReliabilityCases report={report} />);
  await screen.findByText(/10 of 20 held-out plays/);
  expect((screen.getByLabelText("Held-out case category") as HTMLSelectElement).value).toBe("1");
});

test("initial all-case film return preserves the selected nondefault model and evaluation", async () => {
  window.history.replaceState(null,"","/team-stats/?tab=analyst&position=QB");
  const page=structuredClone(producer.page) as unknown as Record<string,unknown>;
  const cases=page.cases as Record<string,unknown>[];
  cases[0].film={clip_id:"exact-clip",focus_seconds:1.25,source_export_job_id:"actual-export-job",source_export_sha256:"a".repeat(64)};
  cases[0].film_unavailable_reason=null;
  api.load.mockResolvedValue(page);
  render(<ModelReliabilityCases report={report} />);
  const link=await screen.findByRole("link",{name:"Watch exact source evidence"});
  const destination=new URL(link.getAttribute("href")!,"https://fixture.invalid");
  const back=new URL(destination.searchParams.get("from")!,"https://fixture.invalid");
  expect(back.pathname).toBe("/team-stats/");
  expect(back.searchParams.get("position")).toBe("QB");
  expect(back.searchParams.get("reliability_model")).toBe(report.model_id);
  expect(back.searchParams.get("reliability_evaluation")).toBe(report.id);
  expect(back.searchParams.get("reliability_case_dimension")).toBe("");
  expect(back.searchParams.get("reliability_case_category")).toBe("");
  expect(back.searchParams.get("reliability_case_offset")).toBe("0");
});

test("missing historical evidence stays unavailable and mismatched offset never renders cases", async () => {
  const view=render(<ModelReliabilityCases report={{id:"old",model_id:"unknown",source:"measured_human_holdout"}} />);
  expect(screen.getByText(/No frozen case evidence was stored/)).toBeTruthy();
  expect(api.load).not.toHaveBeenCalled();
  view.unmount();
  api.load.mockResolvedValue({...producer.page,offset:25});
  render(<ModelReliabilityCases report={report} />);
  expect(await screen.findByRole("alert")).toBeTruthy();
  expect(screen.queryByRole("table")).toBeNull();
});


test("unfiltered later-page URL restores the exact stored selection", async () => {
  const later = { ...report, sample_count:50, heldout_diagnostics:{...report.heldout_diagnostics as Record<string,unknown>,case_count:50} };
  window.history.replaceState(null,"",`/team-stats/?reliability_evaluation=${report.id}&reliability_case_dimension=&reliability_case_category=&reliability_case_offset=25`);
  api.load.mockReturnValue(new Promise(() => {}));
  render(<ModelReliabilityCases report={later} />);
  await waitFor(() => expect(api.load).toHaveBeenCalledWith(report.id,{dimension:"",category:"",offset:25},undefined));
});

test("raw candidate descriptor cannot activate a stored-evaluation case endpoint", () => {
  render(<ModelReliabilityCases report={{...report,id:"candidate-report",source:"worker_report_unverified"}} />);
  expect(screen.getByText(/requires a stored, independently verified evaluation/)).toBeTruthy();
  expect(api.load).not.toHaveBeenCalled();
  expect(screen.queryByRole("button")).toBeNull();
});
