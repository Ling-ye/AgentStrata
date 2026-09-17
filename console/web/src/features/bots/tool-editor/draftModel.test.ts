import { describe, expect, it } from "vitest";
import type { BotToolConfig } from "../../../types";
import { draftReducer, draftIsDirty } from "./draftModel";

const saved: BotToolConfig = { tools: { packs: ["workspace"], features: [], hide: [], mcp: { servers: [{ ref: "docs", enabled: true }] } }, agents: { presets: [], workflows: [] } };
const initial = { saved, draft: saved };
const disable = (config: BotToolConfig): BotToolConfig => ({ ...config, tools: { ...config.tools, mcp: { servers: [{ ref: "docs", enabled: false }] } } });

describe("one instance configuration draft", () => {
  it("keeps cross-layer edits when configuration polling returns", () => {
    const edited = draftReducer(initial, { type: "change", update: disable });
    const refreshed = draftReducer(edited, { type: "receive", config: structuredClone(saved) });
    expect(refreshed.draft?.tools.mcp.servers[0].enabled).toBe(false);
    expect(draftIsDirty(refreshed)).toBe(true);
    expect(saved.tools.mcp.servers[0].enabled).toBe(true);
  });
  it("discards to the latest saved settings and recognizes reverted edits as clean", () => {
    const edited = draftReducer(initial, { type: "change", update: disable });
    expect(draftIsDirty(draftReducer(edited, { type: "discard" }))).toBe(false);
    expect(draftIsDirty(draftReducer(edited, { type: "change", update: () => structuredClone(saved) }))).toBe(false);
  });
  it("only accepts an acknowledged save and preserves newer edits during delayed completion", () => {
    const edited = draftReducer(initial, { type: "change", update: disable });
    // No acknowledgement (including failure) means the edited state stays dirty.
    expect(draftIsDirty(edited)).toBe(true);
    const newer = draftReducer(edited, { type: "change", update: (config) => ({ ...config, agents: { ...config.agents, presets: ["worker"] } }) });
    const completed = draftReducer(newer, { type: "saved", submitted: edited.draft!, config: edited.draft! });
    expect(completed.draft?.agents.presets).toEqual(["worker"]);
    expect(draftIsDirty(completed)).toBe(true);
    expect(draftIsDirty(draftReducer(edited, { type: "saved", submitted: edited.draft!, config: edited.draft! }))).toBe(false);
  });
  it("does not share data between independently mounted instance editors", () => {
    const a = draftReducer(initial, { type: "change", update: disable });
    const b = draftReducer({ saved: null, draft: null }, { type: "receive", config: structuredClone(saved) });
    expect(a.draft?.tools.mcp.servers[0].enabled).toBe(false);
    expect(b.draft?.tools.mcp.servers[0].enabled).toBe(true);
  });
});
