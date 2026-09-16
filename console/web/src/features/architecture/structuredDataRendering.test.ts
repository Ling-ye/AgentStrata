import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import StructuredData, { JsonData } from "./StructuredData";
import { TaskDetailState } from "./observationDetailState";

afterEach(() => vi.unstubAllGlobals());

describe("structured data reading controls", () => {
  it.each([
    { name: "tree", value: { name: "sample", nested: { a: 1 }, empty: "", nil: null } },
    { name: "object array", value: [{ name: "alpha", score: 1 }, { name: "beta", score: null }] },
    { name: "empty array", value: [] },
  ])("has no field or block copy controls in $name mode", ({ value }) => {
    const html = renderToStaticMarkup(createElement(StructuredData, { value }));
    expect(html).not.toContain("复制");
    expect(html).not.toContain("structured-actions");
    expect(html).toContain("搜索已加载数据");
  });
  it("shows exactly one copy control when JSON mode is restored", () => {
    vi.stubGlobal("sessionStorage", { getItem: () => JSON.stringify({ "task/data-mode": 2 }) });
    const html = renderToStaticMarkup(createElement(TaskDetailState, { instanceId: "fixture", runId: "run", children:
      createElement(StructuredData, { value: { name: "sample" } }) }));
    expect(html.match(/复制 JSON/g)).toHaveLength(1);
    expect(html).toContain("structured-json");
    expect(html).not.toContain("structured-row");
  });
  it("always renders object arrays as a tree without table controls", () => {
    const html = renderToStaticMarkup(createElement(StructuredData, { value: [{ name: "alpha" }, { name: "beta" }] }));
    expect(html).toContain('data-json-path="/0"');
    expect(html).toContain('data-json-path="/1"');
    expect(html).toContain("array");
    expect(html).not.toContain("表格");
    expect(html).not.toContain("<table");
  });
  it("nested object arrays expand in place without restoring table actions", () => {
    vi.stubGlobal("sessionStorage", { getItem: () => JSON.stringify({ "task/tree:/items": true, "task/tree:/items/0": true }) });
    const html = renderToStaticMarkup(createElement(TaskDetailState, { instanceId: "fixture", runId: "run", children:
      createElement(StructuredData, { value: { items: [{ name: "alpha" }] } }) }));
    expect(html).toContain('data-json-path="/items/0/name"');
    expect(html).toContain("alpha");
    expect(html).not.toContain("表格");
    expect(html).not.toContain("<table");
  });
  it("uses the default tree for a saved display mode outside the available options", () => {
    vi.stubGlobal("sessionStorage", { getItem: () => JSON.stringify({ "task/data-mode": 1 }) });
    const html = renderToStaticMarkup(createElement(TaskDetailState, { instanceId: "fixture", runId: "run", children:
      createElement(StructuredData, { value: [{ name: "alpha" }] }) }));
    expect(html).toContain('data-json-path="/0"');
    expect(html).not.toContain("表格");
    expect(html).not.toContain("<table");
  });
  it("renders full records as JSON without another tree, table or search toolbar", () => {
    const html = renderToStaticMarkup(createElement(JsonData, { value: { tools: [{ name: "lookup" }], source: "fixture" } }));
    expect(html.match(/复制 JSON/g)).toHaveLength(1);
    expect(html).not.toContain("structured-row");
    expect(html).not.toContain("搜索已加载数据");
    expect(html).not.toContain("structured-table");
    expect(html).toContain("lookup");
  });
  it("provides long JSON expansion without creating another copy control", () => {
    const html = renderToStaticMarkup(createElement(JsonData, { value: { text: "long ".repeat(2000) } }));
    expect(html.match(/复制 JSON/g)).toHaveLength(1);
    expect(html).toContain("展开完整 JSON");
  });
});
