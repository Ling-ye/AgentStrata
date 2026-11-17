export interface PendingApplicationRequest { id: string; text: string }
export function readPendingRequest(storage: Pick<Storage, "getItem">, key: string): PendingApplicationRequest | undefined {
  const raw = storage.getItem(key);
  if (!raw) return undefined;
  const value = JSON.parse(raw) as PendingApplicationRequest;
  if (!value || !/^[a-f0-9]{32}$/.test(value.id) || typeof value.text !== "string" || !value.text.trim()) throw new Error("待确认请求记录无效，请保留记录并检查应用历史，不要重复提交。");
  return value;
}
export function savePendingRequest(storage: Pick<Storage, "setItem" | "removeItem">, key: string, request?: PendingApplicationRequest) {
  if (request) storage.setItem(key, JSON.stringify(request));
  else storage.removeItem(key);
}
export const applicationRequestKey = (kind: "qq-configuration" | "qq-development", application: string) => "agentstrata." + kind + ":" + application;
export function clearAcknowledgedRequest(storage: Pick<Storage, "getItem" | "removeItem">, key: string, request: PendingApplicationRequest) {
  const current = readPendingRequest(storage, key);
  if (!current || current.id !== request.id || current.text !== request.text) return false;
  storage.removeItem(key);
  return true;
}
