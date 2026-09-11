import { afterEach, describe, expect, it, vi } from "vitest";
import { caseInstanceId, selectedInstance } from "./caseInstance";
import { harnessApi, type SourcePreview } from "./api";
import { normalizeTrial } from "../evals/evaluationApi";

const id = `case-${"a".repeat(32)}`;
afterEach(() => vi.unstubAllGlobals());

describe("Case execution instance", () => {
  it("accepts a copied instance ID and preserves it in evaluation rows", () => {
    expect(caseInstanceId(` ${id}\n`)).toBe(id);
    expect(normalizeTrial({ case_instance_id: id }).case_instance_id).toBe(id);
  });
  it.each(["", "eval-example", "case-b", "trial-example", "evalcase:eval/suite%3Ab/main", "case-xyz", `case-${"a".repeat(33)}`])("rejects non-instance input %s", input => {
    expect(() => caseInstanceId(input)).toThrow(/Case 实例 ID/);
  });
  it("checks the returned instance and evaluation bindings", () => {
    const preview: SourcePreview = { kind: "evaluation", evaluation_id: "eval-source", bot_id: "sample", blockers: [], history: [],
      case_instance: { case_instance_id: id, evaluation_id: "eval-source", case_id: "b", case_ref: "suite:b", target_id: "main", trial_id: "trial-b", attempt: 2, outcome: "failed" } };
    expect(selectedInstance(preview, id)?.attempt).toBe(2);
    expect(selectedInstance(preview, `case-${"b".repeat(32)}`)).toBeUndefined();
    expect(selectedInstance({ ...preview, evaluation_id: "other" }, id)).toBeUndefined();
    expect(selectedInstance(undefined, id)).toBeUndefined();
  });
  it("loads and submits only the Case instance ID without client-side routing fields", async () => {
    const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) });
    vi.stubGlobal("fetch", fetch);
    await harnessApi.load("evaluation", id, "");
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ kind: "evaluation", source_id: id, bot_id: "" });
    const body = { source_kind: "evaluation" as const, case_instance_id: id, model: "test-model", reasoning_effort: "medium", max_attempts: 3, timeout_seconds: 7200, request_id: "one" };
    await harnessApi.start(body);
    expect(JSON.parse(fetch.mock.calls[1][1].body)).toEqual(body);
  });
});
