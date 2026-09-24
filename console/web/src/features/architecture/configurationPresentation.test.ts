import { describe, expect, it } from "vitest";
import { CONFIGURATION_LAYERS, configurationViews, configurationLink, configurationSelection, configurationSearch, configurationRelated, configurationSummary } from "./configurationPresentation";
import type { Configuration, InspectionEntity } from "./workbenchModel";
import { botTabFromParams } from "./taskWorkspaceState";

const entity = (id: string, config: Record<string, unknown> = {}, layer = "application"): InspectionEntity => ({ id, layer, name: id,
  configured: true, loaded: null, connected: null, available: null, config });
const input: Configuration = { layers: [], entities: [
  entity("agent:main", { runtime_id: "codex", model: "chat-fixture", reasoning_effort: "medium", model_slot: "chat" }, "agent"),
  entity("agent:delegation", { defaults: { max_tool_calls: 5 } }, "agent"),
  entity("agent:search-budget", { max_tool_calls: 2 }, "agent"),
  entity("subagent:worker", { timeout_seconds: 40 }), entity("mcp:lookup", { catalog_ref: "lookup", command: "fixture" }),
  entity("context:wiki", { enabled: true, root_env: "WIKI_ROOT" }),
  entity("context:playbooks", { manifest: "skills.yaml" }), entity("rag:docs", { path: "docs", include: ["*.md"] }), entity("skill:career", { description: "Skill fixture" }),
  entity("search:brave", { endpoint: "configured" }), entity("pack:dev.tools", { hidden_tools: ["hidden"] }), entity("pack:wiki.knowledge"),
  entity("context:dev", { shell: { timeout_max: 50 } }), entity("context:codebases", { registry: "repos.yaml" }), entity("feature:chat.image_inputs"),
  entity("codebase:sample", { max_read_bytes: 4096 }), { ...entity("tool:lookup", { parameters: { type: "object" } }), configured: null, loaded: true, refs: ["mcp:lookup"] },
  entity("channel:qq", { provider: "onebot_v11" }, "channel"), entity("platform:instance", {}, "channel"), entity("gateway:instance", { protocol_version: 1 }, "gateway"),
  entity("policy:instance", { owner: "fixture" }, "authorization"), entity("workspace:instance", {}, "application"),
  entity("prompts:instance", { identity: "identity.md" }, "agent"), entity("model-slot:chat", { env_prefix: "DEMO" }, "agent"),
  entity("service:instance", {}, "control"), entity("config:instance", {}, "botspec"), entity("protocol:versions", {}, "contracts"),
] };

describe("four-layer configuration ownership", () => {
  it("has exactly four runtime navigation entries and keeps instance metadata outside them", () => {
    expect(CONFIGURATION_LAYERS.map((layer) => layer.id)).toEqual(["channel", "gateway", "application", "agent"]);
    const views = configurationViews(input);
    expect(views.filter((item) => item.displayLayer === null).map((item) => item.id)).toEqual(["service:instance", "config:instance", "protocol:versions"]);
    expect(new Set(views.map((item) => item.viewKey)).size).toBe(views.length);
    expect(new Set(views.map((item) => item.id))).toEqual(new Set(input.entities.map((item) => item.id)));
  });
  it.each([
    ["channel:qq", "channel", "channel"], ["policy:instance", "gateway", "access"], ["gateway:instance", "gateway", "gateway"],
    ["context:wiki", "application", "memory"], ["rag:docs", "application", "memory"],
    ["skill:career", "application", "resources"], ["context:dev", "application", "resources"], ["codebase:sample", "application", "resources"],
    ["feature:chat.image_inputs", "application", "features"], ["prompts:instance", "agent", "prompts"], ["mcp:lookup", "agent", "mcp"],
    ["pack:dev.tools", "agent", "packs"], ["tool:lookup", "agent", "tools"], ["search:brave", "agent", "search"],
  ])("maps %s to its responsibility without rewriting the original layer", (id, layer, group) => {
    const row = configurationViews(input).find((item) => item.id === id)!;
    expect([row.displayLayer, row.group]).toEqual([layer, group]);
    expect(row.layer).toBe(input.entities.find((item) => item.id === id)!.layer);
  });
  it("keeps normalized fields in one location without changing source or historical snapshots", () => {
    const original = JSON.stringify(input);
    const rows = configurationViews(input);
    expect(rows.filter((item) => item.id === "agent:main")).toHaveLength(1);
    expect(rows.find((item) => item.id === "agent:delegation")?.group).toBe("delegation");
    expect(rows.find((item) => item.id === "agent:search-budget")?.group).toBe("search");
    expect(configurationSummary(rows[0])).toContain("实例默认模型 chat-fixture / medium");
    expect(JSON.stringify(input)).toBe(original);
  });
  it("shows unconfigured resources and marks unavailable platform adapters separately", () => {
    const rows = configurationViews({ ...input, entities: [...input.entities, entity("context:rag", { sources: null })] });
    expect(rows.find((item) => item.id === "platform:instance")?.applicable).toBe(false);
    expect(rows.find((item) => item.id === "context:rag")?.applicable).toBe(true);
    expect(configurationSummary(rows.find((item) => item.id === "platform:instance")!)).toBe("当前实例不适用");
  });
  it("keeps research overrides and custom agents independent of editable preset membership", () => {
    const research = { ...entity("model-slot:research", { model: "default" }), effective_config: { model: "override" },
      effective_environment: { DEMO_RESEARCH_MODEL: "override" }, field_sources: { model: "环境覆盖 DEMO_RESEARCH_MODEL" } };
    const custom = { ...entity("subagent:custom", { model: "custom-model" }), membership: "custom" as const };
    const draft = { tools: { packs: [], features: [], hide: [], mcp: { servers: [] } }, agents: { presets: [], workflows: [] } };
    const rows = configurationViews({ layers: [], entities: [research, custom] }, draft);
    expect(configurationSummary(rows[0])).toBe("override");
    expect(rows[0].environment).toEqual({ DEMO_RESEARCH_MODEL: "override" });
    expect(rows[1].id).toBe("subagent:custom");
  });
  it("projects changes across layers from one draft while keeping observed dynamic tools", () => {
    const draft = { tools: { packs: ["new-pack"], features: ["images"], hide: ["hidden"], mcp: { servers: [{ ref: "lookup", enabled: false }] } }, agents: { presets: ["new-worker"], workflows: [] } };
    const rows = configurationViews(input, draft);
    expect(rows.find((item) => item.id === "mcp:lookup")?.configured).toBe(false);
    expect(rows.some((item) => item.id === "pack:dev.tools")).toBe(false);
    expect(rows.find((item) => item.id === "pack:new-pack")?.displayLayer).toBe("agent");
    expect(rows.find((item) => item.id === "feature:images")?.displayLayer).toBe("application");
    expect(rows.some((item) => item.id === "subagent:worker")).toBe(false);
    expect(rows.some((item) => item.id === "subagent:new-worker")).toBe(true);
    expect(rows.find((item) => item.id === "tool:lookup")?.configured).toBeNull();
    const hidden = { ...entity("tool:hidden"), configured: false };
    const hiddenRows = configurationViews({ layers: [], entities: [hidden] }, { ...draft, tools: { ...draft.tools, hide: [] } });
    expect(hiddenRows[0].configured).toBe(true);
  });
});

describe("configuration navigation and search", () => {
  const rows = configurationViews(input);
  it("defaults to Channel and restores a previously selected layer", () => {
    expect(configurationSelection(rows, new URLSearchParams()).layer).toBe("channel");
    expect(configurationSelection(rows, new URLSearchParams(), "agent").layer).toBe("agent");
    expect(configurationSelection(rows, new URLSearchParams(), "missing").layer).toBe("channel");
  });
  it("uses entity ownership over a mismatching layer and disambiguates split fields", () => {
    const params = new URLSearchParams(configurationLink("bot", "agent:delegation", "delegation").split("?")[1]);
    params.set("layer", "channel");
    expect(configurationSelection(rows, params).selected?.viewKey).toBe("delegation:agent:delegation");
    expect(configurationSelection(rows, params).layer).toBe("agent");
    expect(botTabFromParams(params)).toBe("configuration");
  });
  it("locates resource links in Application, instance info outside navigation and reports missing entities", () => {
    expect(configurationSelection(rows, new URLSearchParams(configurationLink("bot", "skill:career").split("?")[1])).layer).toBe("application");
    expect(configurationSelection(rows, new URLSearchParams("entity=service:instance")).selected?.displayLayer).toBeNull();
    expect(configurationSelection(rows, new URLSearchParams("entity=tool:missing")).missing).toBe(true);
    expect(botTabFromParams(new URLSearchParams("tab=capabilities&entity=tool:lookup"))).toBe("tasks");
  });
  it("searches fields, values and resolved environment across all layers", () => {
    expect(configurationSearch(rows, "max_read_bytes").map((item) => item.id)).toEqual(["codebase:sample"]);
    expect(configurationSearch(rows, " FIXTURE ").map((item) => item.id)).toEqual(["agent:main", "mcp:lookup", "skill:career", "policy:instance"]);
    expect(configurationSearch(rows, "nonexistent")).toEqual([]);
    expect(configurationSearch(rows, " ")).toEqual([]);
    expect(configurationSearch([{ ...rows[0], environment: { DEMO_MODEL: "unique-model" } }], "unique-model")).toHaveLength(1);
  });
  it("links MCP/tool and resource/tool-pack relationships in both directions", () => {
    for (const [source, target] of [["mcp:lookup", "tool:lookup"], ["tool:lookup", "mcp:lookup"], ["context:wiki", "pack:wiki.knowledge"], ["pack:wiki.knowledge", "context:wiki"]]) {
      expect(configurationRelated(rows, rows.find((item) => item.id === source)!).map((item) => item.id)).toContain(target);
    }
  });
});
