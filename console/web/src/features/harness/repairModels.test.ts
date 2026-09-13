import { describe, expect, it } from "vitest";
import type { Configuration } from "../architecture/workbenchModel";
import { repairModels } from "./repairModels";

function configuration(environment: Record<string, unknown> = {}): Configuration {
  return { layers: [], entities: [{ id: "model-slot:code", layer: "agent", name: "Codex",
    configured: true, loaded: null, connected: null, available: null,
    config: { model: "declared-model" },
    effective_config: { model: "current-model", profiles: {
      duplicate: { model: "current-model", reasoning_effort: "medium" },
      alternate: { model: "alternate-model", reasoning_effort: "max" },
    } }, effective_environment: environment }] };
}

describe("Harness repair model choices", () => {
  it("selects the current effective model and deduplicates configured profile models", () => {
    const result = repairModels(configuration());
    expect(result.defaultModel).toBe("current-model");
    expect(result.options).toEqual([
      { value: "current-model", label: "current-model（机器人当前配置）" },
      { value: "alternate-model", label: "alternate-model" },
    ]);
    expect(result.error).toBe("");
  });

  it("honors instance environment profile overrides instead of stale YAML profiles", () => {
    const result = repairModels(configuration({ EXAMPLE_CODE_PROFILES_JSON:
      JSON.stringify({ overridden: { model: "environment-model" } }) }));
    expect(result.options.map(option => option.value)).toEqual(["current-model", "environment-model"]);
  });

  it("keeps only the current model when the effective profile override is empty", () => {
    expect(repairModels(configuration({ EXAMPLE_CODE_PROFILES_JSON: "{}" })).options)
      .toHaveLength(1);
  });

  it("reports invalid profile JSON so the form cannot submit a stale selection", () => {
    const result = repairModels(configuration({ EXAMPLE_CODE_PROFILES_JSON: "invalid json" }));
    expect(result.error).toBe("机器人模型档案配置无效");
    expect(result.options).toEqual([]);
  });

  it("does not invent a default when current configuration is unavailable", () => {
    expect(repairModels(null).defaultModel).toBe("");
    expect(repairModels(null).error).toBeTruthy();
    const value = configuration();
    value.entities[0].effective_config = null;
    expect(repairModels(value).options).toEqual([]);
  });
});
