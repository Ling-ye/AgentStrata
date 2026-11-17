export interface Live2DImportStatus {
  id: string; state: "pending" | "selecting" | "importing" | "imported" | "cancelled" | "failed" | "recovery_required";
  application_id: string | null; message: string; retryable: boolean; requires_selection_closed?: boolean;
}
export const live2dImportKey = "agentstrata.live2d-import";
export function readLive2DImport(storage: Pick<Storage, "getItem">) {
  const value = storage.getItem(live2dImportKey);
  if (value && !/^[a-f0-9]{32}$/.test(value)) throw new Error("导入请求记录无效，请先核对应用列表，不要重复导入。");
  return value || undefined;
}
async function request(path: string, options?: RequestInit): Promise<Live2DImportStatus> {
  const response = await fetch("/api/applications/imports" + path, { cache: "no-store", ...options, headers: { "Content-Type": "application/json", ...options?.headers } });
  const value = await response.json();
  if (!response.ok) throw new Error(typeof value.detail === "string" ? value.detail : "无法核对桌面伙伴导入，请保留原请求并重试。");
  return value;
}
export const live2dImportApi = {
  start: (id: string) => request("/live2d", { method: "POST", body: JSON.stringify({ id }) }),
  status: (id: string, signal?: AbortSignal) => request("/" + encodeURIComponent(id), { signal }),
  retry: (id: string, selectionClosed = false) => request("/" + encodeURIComponent(id) + "/retry", { method: "POST", body: JSON.stringify(selectionClosed ? { selection_closed: true } : {}) }),
};
export function importStateLabel(state: string) {
  return ({ pending: "等待本机选择", selecting: "正在选择或识别桌面伙伴", importing: "正在校验与导入基线", imported: "公开基线已导入", cancelled: "选择已取消", failed: "导入失败", recovery_required: "需要核对中断的导入" } as Record<string, string>)[state] || "状态待核对";
}
export function canStartNewImport(status?: Live2DImportStatus) {
  return status?.state === "cancelled" || status?.state === "failed";
}
