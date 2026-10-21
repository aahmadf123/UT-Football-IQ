import { afterEach, expect, test, vi } from "vitest";
import producer from "@/components/__fixtures__/model-reliability-cases-producer.json";
import { loadHeldoutCases, validCasePage } from "./model-reliability-cases";
import type { LearningEvaluation } from "./types";

afterEach(() => vi.unstubAllGlobals());
test("case read encodes categories and authenticates the existing backend",async () => {
 const fetch=vi.fn().mockResolvedValue(new Response(JSON.stringify(producer.page),{status:200}));vi.stubGlobal("fetch",fetch);
 await loadHeldoutCases(producer.evaluation.id,{dimension:"source_group",category:"session / alpha",offset:25},"staff-token");
 const url=new URL(String(fetch.mock.calls[0][0]),"http://fixture.invalid");
 expect(url.searchParams.get("category")).toBe("session / alpha");expect(url.searchParams.get("offset")).toBe("25");
 expect(fetch.mock.calls[0][1].headers).toEqual({Authorization:"Bearer staff-token"});
});
test("case validator refuses foreign evidence, fractions, mismatched offsets and fabricated residuals",() => {
 const report=producer.evaluation as LearningEvaluation;
 expect(validCasePage(producer.page,report,"","",0)).toBe(true);
 for(const key of ["model_id","artifact_sha256","case_sha256"]) expect(validCasePage({...producer.page,[key]:"foreign"},report,"","",0)).toBe(false);
 expect(validCasePage({...producer.page,selected_count:19.5},report,"","",0)).toBe(false);
 expect(validCasePage({...producer.page,offset:25},report,"","",0)).toBe(false);
 expect(validCasePage({...producer.page,minimum_sample_count:1},report,"","",0)).toBe(false);
 expect(validCasePage({...producer.selected,small_sample:false},report,"observed_outcome","1",0)).toBe(false);
 expect(validCasePage({...producer.page,cases:producer.page.cases.slice(0,2)},report,"","",0)).toBe(false);
 const wrongGroups=structuredClone(producer.page);wrongGroups.groups.observed_outcome[0].case_count+=1;expect(validCasePage(wrongGroups,report,"","",0)).toBe(false);
 const edited=structuredClone(producer.page);edited.cases[0].error=99;expect(validCasePage(edited,report,"","",0)).toBe(false);
});
