import { describe, expect, it } from "vitest";
import { filterSpans, spanDepth, type TraceSpan } from "./model";
const spans: TraceSpan[] = [
  { id: "a", name: "agent", type: "agent", status: "completed", start_time: "", order: 0 },
  { id: "b", parent_id: "a", name: "read_file", type: "tool", status: "failed", start_time: "", order: 1 },
];
describe("local trace presentation", () => {
  it("keeps parent depth and handles a page without its parent", () => {
    expect(spanDepth(spans[1], spans)).toBe(1);
    expect(spanDepth(spans[1], [spans[1]])).toBe(0);
  });
  it("filters actual error states and names", () => {
    expect(filterSpans(spans, "READ", "tool", true)).toEqual([spans[1]]);
    expect(filterSpans(spans, "", "agent", true)).toEqual([]);
  });
});
