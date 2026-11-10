export interface SourceState {
  state: "unprepared" | "prepared" | "installed" | "stale" | "blocked";
  source_digest?: string;
  definition_matches: boolean;
  message: string;
}
export interface DevelopmentTask { task_id: string; status: string; can_install: boolean; can_cancel: boolean }
async function request<T>(id: string, suffix = "", options?: RequestInit): Promise<T> {
  const result = await fetch("/api/applications/" + encodeURIComponent(id) + "/development" + suffix,
    { cache: "no-store", ...options, headers: { "Content-Type": "application/json", ...options?.headers } });
  const value = await result.json();
  if (!result.ok) throw new Error(typeof value.detail === "string" ? value.detail : "应用开发操作失败，请核对原请求状态");
  return value;
}
export const developmentApi = {
  status: (id: string, signal?: AbortSignal) => request<SourceState>(id, "", { signal }),
  prepare: (id: string) => request<SourceState>(id, "/prepare", { method: "POST", body: "{}" }),
  tasks: (id: string, signal?: AbortSignal) => request<{ tasks: DevelopmentTask[]; total: number }>(id, "/tasks", { signal }),
  start: (id: string, requestId: string, goal: string) => request<DevelopmentTask>(id, "/tasks", { method: "POST", body: JSON.stringify({ id: requestId, goal }) }),
  action: (id: string, task: string, action: "cancel" | "install") => request<SourceState | DevelopmentTask>(id, "/tasks/" + encodeURIComponent(task) + "/" + action, { method: "POST", body: "{}" }),
};
export function sourceStateLabel(state: string) {
  return ({ unprepared: "尚未准备源码", prepared: "独立基线已准备", installed: "验收版本已安装", stale: "源码或配置已变化", blocked: "暂不可用" } as Record<string,string>)[state] || "状态待核对";
}
export function developmentTaskLabel(state: string) {
  return ({ fixed: "独立验收通过", failed: "未完成", cancelled: "已取消", canceled: "已取消", blocked: "需要处理", stopped: "已停止", needs_review: "需要独立复核", no_changes: "无需改动", not_reproduced: "未复现目标差异", interrupted: "已中断", cancel_requested: "正在请求取消", queued: "等待执行", running: "正在执行", preparing: "正在准备", processing: "正在处理" } as Record<string,string>)[state] || "状态待核对";
}
