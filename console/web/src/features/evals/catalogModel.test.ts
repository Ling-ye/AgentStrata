import { describe, expect, it } from "vitest";
import { catalogGroups, subjectForRecord, suiteFormKey } from "./catalogModel";
import { normalizeEvaluation, type EvaluationSubject, type EvaluationSuite } from "./model";

function suite(id: string, subject: EvaluationSubject, source = "public_benchmark", implemented = true) {
  return { suite_id: id, subject_type: subject, source_type: source, implemented,
    capability_tags: ["工具调用", "信息检索"], benchmark: { case_set_hash: "set-a" } } as EvaluationSuite;
}

describe("subject catalog", () => {
  it("uses metadata for subjects and sources, including new suite IDs", () => {
    const suites = [suite("bfcl", "model"), suite("ifeval", "agent"), suite("new-suite", "agent", "project"), suite("qq", "system", "project")];
    expect(catalogGroups(suites, "model").flatMap(g => g.suites.map(s => s.suite_id))).toEqual(["bfcl"]);
    expect(catalogGroups(suites, "agent").map(g => [g.id, g.suites[0].suite_id])).toEqual([["project", "new-suite"], ["public_benchmark", "ifeval"]]);
    expect(catalogGroups(suites, "system")[0].suites[0].suite_id).toBe("qq");
  });
  it("separates planned suites without hiding preparation blockers and supports multiple tags", () => {
    const readyLater = { ...suite("data-needed", "agent"), ready: false };
    const planned = suite("planned", "agent", "public_benchmark", false);
    expect(catalogGroups([readyLater, planned], "agent", "信息检索")[0]).toMatchObject({ suites: [readyLater], planned: [planned] });
    expect(catalogGroups([readyLater], "agent", "代码修复")).toEqual([]);
  });
  it("remounts the form across Bot, subject, suite and dataset changes", () => {
    const original = suite("same-case-ids", "agent");
    const key = suiteFormKey("bot-a", original);
    for (const [bot, changed] of [
      ["bot-b", original], ["bot-a", { ...original, subject_type: "model" }],
      ["bot-a", { ...original, suite_id: "another-suite" }],
      ["bot-a", { ...original, benchmark: { case_set_hash: "set-b" } }],
    ] as Array<[string, EvaluationSuite]>) expect(suiteFormKey(bot, changed)).not.toBe(key);
  });
  it("reads historical subject only from the frozen snapshot, never suite ID or current driver", () => {
    const record = normalizeEvaluation({ evaluation_id: "old", kind: "suite", suite_id: "bfcl", benchmark: { executor: { driver: "direct_llm" } } });
    expect(subjectForRecord(record)).toBeNull();
    record.benchmark = { ...record.benchmark, subject_type: "agent" };
    expect(subjectForRecord(record)?.id).toBe("agent");
    record.benchmark = { subject_type: "unknown" };
    expect(subjectForRecord(record)).toBeNull();
  });
});
