import { expect, it } from "vitest";
import ELK from "elkjs/lib/elk.bundled.js";
import { executionLayoutGraph } from "./executionLayout";

it("lays out a thousand-call high-fanout Agent without discarding nodes", async () => {
  const nodes = ["stage", "agent", ...Array.from({ length: 1000 }, (_, index) => `tool-${index}`)];
  const edges = [["root", "stage", "agent"], ...nodes.slice(2).map((id) => [`edge-${id}`, "agent", id])];
  const result = await new ELK().layout(executionLayoutGraph(nodes, edges));
  expect(result.children).toHaveLength(1002);
  expect(result.edges).toHaveLength(1001);
  expect(result.children?.every((node) => Number.isFinite(node.x) && Number.isFinite(node.y))).toBe(true);
  expect(new Set(result.children?.map((node) => `${node.x}:${node.y}`)).size).toBe(1002);
}, 10000);
