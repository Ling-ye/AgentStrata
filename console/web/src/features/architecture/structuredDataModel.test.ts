import { describe, expect, it } from "vitest";
import { pointerChild, searchData, valueSummary, valueType } from "./structuredDataModel";

describe("typed structured payloads", () => {
  it("distinguishes null, missing, empty strings, containers and scalar types", () => {
    expect([undefined, null, "", [], {}, 0, false].map(valueSummary)).toEqual(["字段缺失", "null", '""', "[]", "{}", "0", "false"]);
    expect([undefined, null, "", [], {}, 0, false].map(valueType)).toEqual(["missing", "null", "string", "array", "object", "number", "boolean"]);
  });
  it("builds reversible JSON pointers using raw keys, including slashes and tildes", () => {
    expect(pointerChild(pointerChild("", "a/b"), "~name")).toBe("/a~1b/~0name");
    expect(pointerChild("/effective_messages", 3)).toBe("/effective_messages/3");
  });
  it("searches nested keys and scalar values with their exact paths", () => {
    expect(searchData({ tools: [{ "a/b": "MATCH" }], matching: null }, "match").matches.map((match) => match.path))
      .toEqual(["/tools/0/a~1b", "/matching"]);
  });
  it("marks bounded results explicitly and never mutates source data", () => {
    const source = { array: Array.from({ length: 120 }, (_, index) => `item-${index}`) };
    const before = JSON.stringify(source);
    const result = searchData(source, "item");
    expect(result.matches).toHaveLength(100); expect(result.more).toBe(true);
    expect(JSON.stringify(source)).toBe(before);
    expect(searchData(source, "item-119").matches[0].path).toBe("/array/119");
  });
  it("traverses deeply nested payloads without recursive search overflow", () => {
    let value: unknown = "target";
    for (let index = 0; index < 2000; index++) value = { child: value };
    expect(searchData(value, "target").matches).toHaveLength(1);
  });
});
