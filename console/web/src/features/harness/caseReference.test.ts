import { describe, expect, it } from "vitest";
import { parseCaseReference, selectedCase } from "./caseReference";
import type { SourcePreview } from "./api";

describe("single Case reference", () => {
  it("decodes each field once and preserves reserved characters and Unicode", () => {
    expect(parseCaseReference("  evalcase:eval%2Fexample/suite%3A%E9%A2%98%E7%9B%AE%2F%25%3F%23/target%252Fone  ")).toEqual({
      evaluation_id: "eval/example", case_ref: "suite:题目/%?#", target_id: "target%2Fone",
    });
  });

  it.each([
    "", "eval-example", "case-b", "suite:case-b", "trial-example", "evalcase:eval-example",
    "evalcase:eval-example/suite%3Acase-b", "evalcase:/suite%3Acase-b/main", "evalcase:eval-example//main",
    "evalcase:eval-example/suite%3Acase-b/", "evalcase:eval-example/suite%3Acase-b/main/extra",
    "evalcase:eval-example/%20/main", "evalcase:eval-example/suite%/main", "evalcase:eval-example/case/%GG",
    "evalcase:eval-example/%E9/main",
  ])("rejects an incomplete or malformed reference: %s", input => {
    expect(() => parseCaseReference(input)).toThrow(/单 Case 引用/);
  });

  const preview: SourcePreview = {
    kind: "evaluation", evaluation_id: "eval-source", bot_id: "sample", blockers: [], history: [],
    failures: [
      { case_ref: "suite:first", case_id: "first", target_id: "main" },
      { case_ref: "suite:second", case_id: "second", target_id: "main" },
      { case_ref: "suite:second", case_id: "second", target_id: "another" },
    ],
  };

  it("selects exactly one Case and Target from the referenced evaluation", () => {
    const reference = parseCaseReference("evalcase:eval-source/suite%3Asecond/another");
    expect(selectedCase(preview, reference)).toEqual(preview.failures![2]);
    expect(selectedCase({ ...preview, evaluation_id: "eval-other" }, reference)).toBeUndefined();
    expect(selectedCase({ ...preview, kind: "robot_task" }, reference)).toBeUndefined();
  });

  it("never substitutes a different Case or Target when the reference has no failure", () => {
    for (const value of ["evalcase:eval-source/suite%3Amissing/main", "evalcase:eval-source/suite%3Asecond/missing", "evalcase:eval-source/second/main"]) {
      expect(selectedCase(preview, parseCaseReference(value))).toBeUndefined();
    }
    const reference = parseCaseReference("evalcase:eval-source/suite%3Asecond/main");
    expect(selectedCase({ ...preview, failures: [] }, reference)).toBeUndefined();
    expect(selectedCase(undefined, reference)).toBeUndefined();
    expect(selectedCase(preview, undefined)).toBeUndefined();
  });
});
