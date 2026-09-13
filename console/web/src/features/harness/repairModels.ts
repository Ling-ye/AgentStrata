import type { Configuration } from "../architecture/workbenchModel";

export function repairModels(configuration: Configuration | null | undefined) {
  const code = configuration?.entities.find(entity => entity.id === "model-slot:code");
  const config = code?.effective_config;
  const defaultModel = typeof config?.model === "string" ? config.model.trim() : "";
  if (!defaultModel) return { defaultModel: "", options: [], error: "无法读取机器人当前配置的修复模型" };

  const encodedProfiles = Object.entries(code?.effective_environment ?? {})
    .find(([key]) => key.endsWith("_CODE_PROFILES_JSON"))?.[1];
  let profiles = config?.profiles;
  if (typeof encodedProfiles === "string" && encodedProfiles.trim()) {
    try { profiles = JSON.parse(encodedProfiles); }
    catch { return { defaultModel: "", options: [], error: "机器人模型档案配置无效" }; }
  }
  const models = new Set([defaultModel]);
  if (profiles && typeof profiles === "object" && !Array.isArray(profiles)) {
    for (const profile of Object.values(profiles)) {
      if (profile && typeof profile === "object" && typeof profile.model === "string" && profile.model.trim()) {
        models.add(profile.model.trim());
      }
    }
  }
  return { defaultModel, options: [...models].map(value => ({ value,
    label: value === defaultModel ? `${value}（机器人当前配置）` : value })), error: "" };
}
