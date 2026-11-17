export interface DevelopmentSetup {
  configured: boolean;
  bindings: { chat: boolean; harness: boolean; "evaluation.judge": boolean };
  auth: Array<{ lane: "main" | "worker"; state: string; login_available: boolean; error_code?: string | null }>;
  login_available: boolean;
}
async function request<T>(path = "", options?: RequestInit): Promise<T> {
  const response = await fetch("/api/development/setup" + path, { cache: "no-store", ...options, headers: { "Content-Type": "application/json", ...options?.headers } });
  const value = await response.json();
  if (!response.ok) throw new Error(typeof value.detail === "string" ? value.detail : "无法读取开发环境状态，请重试。");
  return value;
}
export const developmentSetupApi = {
  status: (signal?: AbortSignal) => request<DevelopmentSetup>("", { signal }),
  login: (lane: "main" | "worker") => request<{ message: string }>("/login/" + lane, { method: "POST", body: "{}" }),
};
export function credentialStateLabel(state: string) {
  return ({ unconfigured: "尚未配置此订阅通道", missing: "尚未授权", recognized: "已识别凭据", ready: "凭据已准备", invalid: "凭据无效", busy: "授权处理中" } as Record<string, string>)[state] || "状态待核对";
}
