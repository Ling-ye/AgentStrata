import { describe, expect, it } from "vitest";
import { configurationViews, configurationTabForEntity } from "./configurationPresentation";
import type { Configuration, InspectionEntity } from "./workbenchModel";
import { botTabFromParams } from "./taskWorkspaceState";

const entity = (id: string, config: Record<string, unknown> = {}): InspectionEntity => ({ id, layer: "application", name: id,
  configured: true, loaded: null, connected: null, available: null, config });
const input: Configuration = { layers: [], entities: [
  entity("agent:main", { backend: "codex", defaults: { max_tool_calls: 5 }, include: ["worker"], overrides: { worker: { timeout_seconds: 40 } }, search_providers: [{ id: "brave" }], search_budget: { max_tool_calls: 2 }, codex: { owner_access: "worktree" } }),
  entity("subagent:worker", { timeout_seconds: 40 }), entity("mcp:lookup", { catalog_ref: "lookup", command: "fixture" }),
  entity("context:wiki", { enabled: true, root_env: "WIKI_ROOT" }), entity("context:memory_store", { provider: "file" }),
  entity("rag:docs", { path: "docs", include: ["*.md"] }), entity("skill:ai-career-intelligence", { description: "Skill fixture" }),
  entity("search:brave", { endpoint: "configured" }), entity("pack:dev.tools", { hidden_tools: ["hidden"] }),
  entity("context:dev", { shell: { timeout_max: 50 } }), entity("context:codebases", { registry: "repos.yaml" }),
  entity("codebase:sample", { max_read_bytes: 4096 }), { ...entity("tool:lookup", { parameters: { type: "object" } }), configured: null, loaded: true, refs: ["mcp:lookup"] },
] };

describe("foundation and capability ownership", () => {
  it("splits mixed agent objects without mutating snapshots or repeating plugin fields", () => {
    const original = JSON.stringify(input);
    const views = configurationViews(input);
    expect(JSON.stringify(input)).toBe(original);
    const main = views.find((item) => item.group === "model")!;
    expect(main.config).toEqual({ backend: "codex", codex: { owner_access: "worktree" } });
    expect(views.find((item) => item.group === "delegation" && item.id === "agent:main")?.config).toEqual({ defaults: { max_tool_calls: 5 } });
    expect(views.find((item) => item.id === "pack:dev.tools")?.config).toEqual({});
    expect(views.find((item) => item.id === "mcp:lookup")?.group).toBe("mcp");
    expect(views.find((item) => item.id === "context:memory_store")?.group).toBe("memory");
    expect(views.find((item) => item.id === "rag:docs")?.group).toBe("memory");
    expect(views.find((item) => item.id === "skill:ai-career-intelligence")?.group).toBe("skills");
    expect(views.find((item) => item.id === "context:wiki")?.group).toBe("plugins");
    expect(views.find((item) => item.id === "tool:lookup")?.refs).toEqual(["mcp:lookup"]);
  });
  it("projects both sides from one draft while keeping dynamic tools", () => {
    const draft = { tools: { packs: ["new-pack"], features: ["images"], hide: ["hidden"], mcp: { servers: [{ ref: "lookup", enabled: false }] } }, agents: { presets: ["new-worker"], workflows: [] } };
    const views = configurationViews(input, draft);
    expect(views.find((item) => item.id === "mcp:lookup")?.configured).toBe(false);
    expect(views.some((item) => item.id === "pack:dev.tools")).toBe(false);
    expect(views.some((item) => item.id === "pack:new-pack")).toBe(true);
    expect(views.some((item) => item.id === "subagent:worker")).toBe(false);
    expect(views.some((item) => item.id === "subagent:new-worker")).toBe(true);
    expect(views.find((item) => item.id === "tool:lookup")?.configured).toBeNull();
  });
  it.each(["skill:ai-career-intelligence", "search:brave", "context:wiki", "context:dev", "codebase:sample", "tool:lookup"])("routes old foundation links for %s to capabilities", (id) => {
    const params = new URLSearchParams({ instance: "fixture", run: "history", tab: "configuration", entity: id });
    expect(botTabFromParams(params)).toBe("capabilities");
    expect(params.get("run")).toBe("history");
    expect(configurationTabForEntity(id)).toBe("capabilities");
  });
  it.each(["mcp:lookup", "subagent:worker", "context:memory_store", "rag:docs", "policy:instance"])("keeps %s in foundation", (id) => {
    expect(botTabFromParams(new URLSearchParams({ entity: id }))).toBe("configuration");
  });
});
