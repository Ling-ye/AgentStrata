export interface SettingSection { values: Record<string, string>; revision: string; status: "applied" | "pending" | "unknown" }
export interface UserSettings {
  prompts: SettingSection; model: SettingSection;
  models: { id: string; model: string; connection: string }[];
}
export async function managementRequest<T>(id: string, path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`/api/applications/${encodeURIComponent(id)}${path}`, {
    cache: "no-store", ...options, headers: { "Content-Type": "application/json", ...options?.headers },
  });
  const value = await response.json();
  if (!response.ok) throw new Error(typeof value.detail === "string" ? value.detail : value.detail?.message || "应用连接暂不可用，请确认客户端后台在线后重试。");
  return value;
}
export function settingsChanged(saved: Record<string, string>, draft: Record<string, string>) {
  return Object.keys(saved).some(key => saved[key] !== draft[key]);
}
