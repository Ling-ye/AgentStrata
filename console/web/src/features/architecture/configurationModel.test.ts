import { describe, expect, it } from "vitest";
import { latestConfiguration, recordedConfiguration } from "./configurationModel";
import type { Configuration, Inspection, InspectionEntity } from "./workbenchModel";
import type { GatewayObservation } from "./model";

const entity = (id: string, timeout: number): InspectionEntity => ({
  id, name: id, layer: "capability", configured: true, loaded: null, connected: null, available: null,
  config: { timeout_seconds: timeout },
});
const config = (model: string, entities: InspectionEntity[]): Configuration => ({ layers: [], entities, model });
const inspection: Inspection = {
  current: config("edited-model", [entity("mcp:search", 30)]),
  loaded: config("running-model", [{ ...entity("mcp:search", 10), loaded: true, connected: false, runtime: { error: "timeout" } },
    { ...entity("tool:lookup", 10), loaded: true }]),
  execution: config("historical-model", [entity("mcp:search", 5), entity("tool:lookup", 5), entity("tool:other", 7)]),
  loaded_meta: { config_id: "runtime", generation: 1, observed_at: 10 },
  loaded_stale: false, pending_changes: true, generated_at: 11, errors: [],
};

describe("current settings and historical configuration text", () => {
  it("shows latest edits immediately while retaining real connection state separately", () => {
    const current = latestConfiguration(inspection)!;
    expect(current.model).toBe("edited-model");
    expect(current.entities[0].config).toEqual({ timeout_seconds: 30 });
    expect(current.entities[0].loaded).toBe(true);
    expect(current.entities[0].connected).toBe(false);
    expect(current.entities[0].runtime).toEqual({ error: "timeout" });
    expect(inspection.current!.entities[0].loaded).toBeNull();
    expect(inspection.loaded!.entities[0].config).toEqual({ timeout_seconds: 10 });
  });

  it("includes observed runtime tools without claiming they are declared in the latest settings", () => {
    const runtimeTool = latestConfiguration(inspection)!.entities.find((item) => item.id === "tool:lookup")!;
    expect(runtimeTool.loaded).toBe(true);
    expect(runtimeTool.configured).toBeNull();
    expect(runtimeTool.available).toBeNull();
  });

  it.each([null, inspection.loaded])("never presents absent or stale connection data as current", (loaded) => {
    const current = latestConfiguration({ ...inspection, loaded, loaded_stale: true })!;
    expect(current.entities[0].config).toEqual(inspection.current!.entities[0].config);
    expect(current.entities.every((entity) => entity.loaded == null && entity.connected == null)).toBe(true);
    if (loaded) expect(current.entities.find((entity) => entity.id === "tool:lookup")?.runtime_stale).toBe(true);
  });

  it("does not fill failed current configuration reads with an older snapshot", () => {
    expect(latestConfiguration({ ...inspection, current: null })).toBeNull();
  });

  it("keeps historical text fixed when current settings or runtime objects change", () => {
    expect(recordedConfiguration(inspection)).toBe(inspection.execution);
    expect(recordedConfiguration({ ...inspection, current: config("new", []), loaded: config("new-runtime", []) }))
      .toBe(inspection.execution);
    expect(recordedConfiguration({ ...inspection, execution: null })).toBeNull();
  });

  it("selects step configuration only by recorded component IDs, not shared names", () => {
    const event: GatewayObservation = { seq: 7, created_at: 10, kind: "ToolStarted", name: "other", entity_id: "tool:lookup", refs: ["mcp:search"] };
    const snapshot = recordedConfiguration(inspection, event)!;
    expect(snapshot.entities.map((item) => item.id)).toEqual(["mcp:search", "tool:lookup"]);
    expect(snapshot.entities.every((item) => item.config?.timeout_seconds === 5)).toBe(true);
    expect(recordedConfiguration(inspection, { ...event, entity_id: "tool:missing", refs: [] })!.entities).toEqual([]);
  });
});
