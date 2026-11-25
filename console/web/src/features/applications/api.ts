import type { EvaluationRecord } from "../evals/model";
import type { RepairTask } from "../harness/api";
export interface ApplicationView {
  first_plan?: boolean; plan_state?: string; icon?: string;
  id: string; name: string; type: string; source: string; instance_id?: string | null; project_id?: string | null;
  capabilities: { local_deploy?: boolean; configure: boolean; control: boolean; chat: boolean; improve: boolean; records: boolean; settings?: boolean; observation?: boolean };
  links: { configuration?: string; records?: string; eval?: string; harness?: string };
  state: { process: string; channel: string; end_to_end: string }; message?: string; last_activity?: number;
}
export interface Configuration { name: string; instructions: string; app_id: string; app_secret?: string; secret_configured?: boolean; credentials_configured?: boolean }
export interface DraftRequest { id: string; name: string; icon: string; requirement: string }
export interface PlanDelivery { platforms: InstallPlatform[]; entry: string; services: string; bindings: string }
export interface FirstPlan { project: string; name: string; state: string; requirement: string; target: string; scope: string; acceptance: string; delivery: PlanDelivery | null; revision: number; approved: number; reply: string }
export interface ManagementMessage { blocks_development?: boolean; read_only?: boolean; recovery_message?: string; retryable?: boolean; launched?: number | null; build?: string; id: string; text: string; state: string; reply: string; task?: string; proposal?: { name?: string; instructions?: string } }
export interface CheckResult { checks?: { ok: boolean; message: string }[]; message?: string; state?: string }
export class ApplicationRequestError extends Error {
  constructor(message: string, public readonly status: number) { super(message); this.name = "ApplicationRequestError"; }
}
async function request<T>(path = "", options?: RequestInit): Promise<T> {
  const response = await fetch("/api/applications" + path, { cache: "no-store", ...options, headers: { "Content-Type": "application/json", ...options?.headers } });
  const value = await response.json();
  if (!response.ok) throw new ApplicationRequestError(typeof value.detail === "string" ? value.detail : "应用操作失败，请检查连接后重试", response.status);
  return value;
}
const path = (id: string) => "/" + encodeURIComponent(id);
export type InstallPlatform = "windows-x64" | "linux-x64";
export interface InstallerRecord {
  id: string; platform: InstallPlatform; state: "queued" | "building" | "ready" | "failed"; error?: string;
  origin: {kind: "commit" | "candidate"; revision?: string; task?: string}; selection?: string;
  size?: number; sha256?: string; docker?: boolean; requirements?: string[]; components?: string[];
  installation?: { completed_at: number; services_ready: boolean }; updated?: number;
  acceptance?: { candidate_digest: string; verification_digest: string };
}
export interface InstallerCatalog {
  build_platforms?: InstallPlatform[];
  platforms: InstallPlatform[]; docker: boolean; requirements: string[]; commit: string; reason: string; builds: InstallerRecord[];
}
export const openApplicationUrl = (id: string) => "/api/applications" + path(id) + "/open";
export const installerUrl = (id: string, key: string, platform: InstallPlatform) =>
  "/api/applications" + path(id) + "/installers/" + encodeURIComponent(key) + "/" + platform;
export const applicationsApi = {
  installers: (id: string, signal?: AbortSignal) => request<InstallerCatalog>(path(id) + "/installers", {signal}),
  importInstallation: (id: string, key: string, platform: InstallPlatform, receipt: unknown) =>
    request(path(id) + "/installers/" + encodeURIComponent(key) + "/" + platform + "/installation", { method: "POST", body: JSON.stringify(receipt) }),
  installerPlan: (id: string, kind: "commit" | "candidate", revision: string, signal?: AbortSignal) =>
    request<{ recipe: string; platforms: InstallPlatform[] }>(path(id) + "/installer-plan?" + new URLSearchParams({kind, revision}), {signal}),
  buildInstaller: (id: string, platform: InstallPlatform, kind: "commit" | "candidate", revision: string) =>
    request<{id: string}>(path(id) + "/installers", {method: "POST", body: JSON.stringify({platform, kind, revision})}),
  taskFlow: (id: string, task: string, signal?: AbortSignal) => request<{goal:string; candidate_digest?:string; origin:{id?:string;kind?:string;digest?:string;evaluation_id?:string};
    steps:Array<{id:string;title:string;status:string;phase:string}>}>(path(id)+"/project-tasks/"+encodeURIComponent(task)+"/flow",{signal}),
  commandPage: (id:string,task:string,source:string,cursor:string,signal?:AbortSignal) =>
    request<{sources:Array<{id:string;kind:string;number?:number}>;selected:string|null;events:Array<{id:string;program:string;exit_code?:number}>;has_more:boolean;next_cursor:string}>(
      path(id)+"/project-tasks/"+encodeURIComponent(task)+"/commands?"+new URLSearchParams({source_id:source,cursor}),{signal}),
  repairOrigin: (id: string, kind: string, sourceId: string, signal?: AbortSignal) =>
    request<{digest:string; case_ref?:string; outcome?:string}>(path(id) + "/project-repairs/source?" + new URLSearchParams({kind,source_id:sourceId}), {signal}),
  startProjectRepair: (id: string, body: {id:string; kind:string; source_id:string; digest:string; feedback:string}) =>
    request<{message_id?:string;task_id?:string}>(path(id) + "/project-repairs", {method:"POST",body:JSON.stringify(body)}),
  evaluationService: (id: string, start = false, signal?: AbortSignal) =>
    request<{ready:boolean; bot_id?:string; message?:string}>(path(id) + "/evaluation-service", {method:start ? "POST" : "GET", signal}),
  draft: (body: DraftRequest) => request<{ id: string }>("/drafts", { method: "POST", body: JSON.stringify(body) }),
  firstPlan: (id: string, signal?: AbortSignal) => request<FirstPlan>(path(id) + "/first-plan", { signal }),
  reviewPlan: (id: string, action: "edit" | "confirm" | "retry", body: Partial<FirstPlan> & { revision: number }) =>
    request<FirstPlan>(path(id) + "/first-plan/" + action, { method: "POST", body: JSON.stringify(body) }),
  desktopAction: (id: string, message: string, action: "bind" | "launch") => request<{ state: string; message: string }>(path(id) + "/builds/" + encodeURIComponent(message) + "/desktop/" + action, { method: "POST", body: "{}" }),
  readiness: (id: string, signal?: AbortSignal) => request<ImprovementReadiness>(path(id) + "/improvement-readiness", { signal }),
  evaluations: (id: string, offset: number, signal?: AbortSignal) => request<EvaluationRecord[]>(path(id) + "/evaluations?" + new URLSearchParams({ offset: String(offset), limit: "20" }), { signal }),
  harness: (id: string, page: number, signal?: AbortSignal) => request<{ tasks: RepairTask[]; total: number; page: number; limit: number }>(path(id) + "/harness?" + new URLSearchParams({ page: String(page), limit: "20" }), { signal }),
  chat: (id: string, signal?: AbortSignal) => request<TrialChat>(path(id) + "/chat", { signal }),
  sendChat: (id: string, messageId: string, text: string) => request<TrialChat>(path(id) + "/chat", { method: "POST", body: JSON.stringify({ id: messageId, text }) }),
  chatAction: (id: string, messageId: string, action: "retry" | "abort") => request<TrialChat>(path(id) + "/chat/" + encodeURIComponent(messageId) + "/" + action, { method: "POST", body: "{}" }),
  list: (signal?: AbortSignal, archived = false) =>
    request<{ applications: ApplicationView[] }>(archived ? "?archived=true" : "", { signal }),
  setArchived: (id: string, archived: boolean) =>
    request<{ id: string; archived: boolean }>(path(id) + "/archive", { method: "PUT", body: JSON.stringify({ archived }) }),
  get: (id: string, signal?: AbortSignal) => request<ApplicationView>(path(id), { signal }),
  create: (id: string, type: string) => request<ApplicationView>("", { method: "POST", body: JSON.stringify({ id, type }) }),
  configuration: (id: string, signal?: AbortSignal) => request<Configuration>(path(id) + "/configuration", { signal }),
  save: (id: string, body: Configuration) => request<Configuration>(path(id) + "/configuration", { method: "POST", body: JSON.stringify(body) }),
  action: (id: string, action: "check" | "start" | "stop") => request<CheckResult>(path(id) + "/" + action, { method: "POST", body: "{}" }),
  messages: (id: string, signal?: AbortSignal) => request<ManagementMessage[]>(path(id) + "/messages", { signal }),
  submit: (id: string, messageId: string, text: string) => request<{ id: string }>(path(id) + "/messages", { method: "POST", body: JSON.stringify({ id: messageId, text }) }),
  retryDevelopment: (id: string, messageId: string) => request<{ ok: boolean }>(path(id) + "/messages/" + encodeURIComponent(messageId) + "/retry", { method: "POST", body: "{}" }),
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
