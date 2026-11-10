import type { EvaluationRecord } from "../evals/model";
import type { RepairTask } from "../harness/api";
export interface ApplicationView {
  id: string; name: string; type: string; source: string; instance_id?: string | null; project_id?: string | null;
  capabilities: { configure: boolean; control: boolean; chat: boolean; improve: boolean; records: boolean };
  links: { configuration?: string; records?: string; eval?: string; harness?: string };
  state: { process: string; channel: string; end_to_end: string }; message?: string;
}
export interface Configuration { name: string; instructions: string; app_id: string; app_secret?: string; secret_configured?: boolean; credentials_configured?: boolean }
export interface ManagementMessage { build?: string; id: string; text: string; state: string; reply: string; task?: string; proposal?: { name?: string; instructions?: string } }
export interface CheckResult { checks?: { ok: boolean; message: string }[]; message?: string; state?: string }
async function request<T>(path = "", options?: RequestInit): Promise<T> {
  const response = await fetch("/api/applications" + path, { cache: "no-store", ...options, headers: { "Content-Type": "application/json", ...options?.headers } });
  const value = await response.json();
  if (!response.ok) throw new Error(typeof value.detail === "string" ? value.detail : "应用操作失败，请检查连接后重试");
  return value;
}
const path = (id: string) => "/" + encodeURIComponent(id);
export const applicationsApi = {
  desktopAction: (id: string, message: string, action: "bind" | "launch") => request<{ state: string; message: string }>(path(id) + "/builds/" + encodeURIComponent(message) + "/desktop/" + action, { method: "POST", body: "{}" }),
  readiness: (id: string, signal?: AbortSignal) => request<ImprovementReadiness>(path(id) + "/improvement-readiness", { signal }),
  evaluations: (id: string, offset: number, signal?: AbortSignal) => request<EvaluationRecord[]>(path(id) + "/evaluations?" + new URLSearchParams({ offset: String(offset), limit: "20" }), { signal }),
  harness: (id: string, page: number, signal?: AbortSignal) => request<{ tasks: RepairTask[]; total: number; page: number; limit: number }>(path(id) + "/harness?" + new URLSearchParams({ page: String(page), limit: "20" }), { signal }),
  chat: (id: string, signal?: AbortSignal) => request<TrialChat>(path(id) + "/chat", { signal }),
  sendChat: (id: string, messageId: string, text: string) => request<TrialChat>(path(id) + "/chat", { method: "POST", body: JSON.stringify({ id: messageId, text }) }),
  chatAction: (id: string, messageId: string, action: "retry" | "abort") => request<TrialChat>(path(id) + "/chat/" + encodeURIComponent(messageId) + "/" + action, { method: "POST", body: "{}" }),
  list: (signal?: AbortSignal) => request<{ applications: ApplicationView[] }>("", { signal }),
  get: (id: string, signal?: AbortSignal) => request<ApplicationView>(path(id), { signal }),
  create: (id: string, type: string) => request<ApplicationView>("", { method: "POST", body: JSON.stringify({ id, type }) }),
  configuration: (id: string, signal?: AbortSignal) => request<Configuration>(path(id) + "/configuration", { signal }),
  save: (id: string, body: Configuration) => request<Configuration>(path(id) + "/configuration", { method: "POST", body: JSON.stringify(body) }),
  action: (id: string, action: "check" | "start" | "stop") => request<CheckResult>(path(id) + "/" + action, { method: "POST", body: "{}" }),
  messages: (id: string, signal?: AbortSignal) => request<ManagementMessage[]>(path(id) + "/messages", { signal }),
  submit: (id: string, messageId: string, text: string) => request<{ id: string }>(path(id) + "/messages", { method: "POST", body: JSON.stringify({ id: messageId, text }) }),
  apply: (id: string, messageId: string) => request<Configuration>(path(id) + "/proposals/" + encodeURIComponent(messageId) + "/apply", { method: "POST", body: "{}" }),
};

export interface TrialMessage { id: string; text: string; state: string; reply?: string; error_code?: string; updated?: number }
export interface TrialChat { available: boolean; reason?: string; messages: TrialMessage[] }
export const applicationBuildUrl = (id: string, message: string) => "/api/applications/" + encodeURIComponent(id) + "/builds/" + encodeURIComponent(message);

export interface ImprovementReadiness {
  application_id: string;
  scope: "source_registration";
  source_supported: boolean;
  scoped_prompt_supported?: boolean;
  execution_verified: boolean;
  checks: Array<{ code: string; status: "passed" | "blocked" | "not_checked"; message: string }>;
  requirements: Array<{ code: string; message: string }>;
}