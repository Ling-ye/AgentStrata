import { harnessRequest, type ProgressTask, type Review } from "../harness/api";

export type Scope = "all" | "runtime" | "console" | "docs";
export interface Rule { id: string; title: string; detector: string; reference: string; guidance: string }
export interface HealthConfig {
  rules: Rule[]; scopes: Array<{ value: Scope; label: string }>; default_model: string; base_commit: string;
  defaults: { reasoning_effort: string; max_attempts: number; timeout_seconds: number };
}
export interface Finding {
  id: string; rule_id: string; path: string; line: number; summary: string; evidence: string;
  recommendation: string; detector: string; disposition: "candidate" | "needs_decision";
}
export interface Check { name: string; status?: string; exit_code: number; log: string; failed_ids?: string[]; existing_failure?: boolean }
export interface HealthAttempt {
  number: number; status: string; changed_files?: string[]; error?: string; patch_sha256?: string;
  review?: Review; verification?: { profile: string; passed: boolean; checks: Check[]; accepted?: boolean; retained_failures?: string[] };
  after?: { checks: Check[]; findings: Finding[] };
}
export interface HealthTask extends ProgressTask {
  base_commit: string; created_at: number; updated_at: number; message?: string; error_code?: string;
  source: { kind: "code_health"; scope: Scope; snapshot_digest: string };
  branch?: string; worktree?: string; candidate_available?: boolean; attempts?: HealthAttempt[];
  check_logs?: Check[];
  governance?: { before: { checks: Check[]; findings: Finding[] }; findings: Finding[];
    selected_ids?: string[]; resolved_ids?: string[]; audit_summary?: string; inspected_paths?: string[] };
}
export type StartHealth = ProgressTask["options"] & { scope: Scope; request_id: string };
export const healthLabels: Record<string, string> = {
  queued: "等待启动", running: "执行中", cancel_requested: "正在取消", fixed: "已验证，待人工提交",
  not_reproduced: "扫描完成", failed: "候选未通过", blocked: "受阻", cancelled: "已取消", interrupted: "已中断",
};
export function healthStatus(task: Pick<HealthTask, "status" | "candidate_available">): string {
  if (task.status === "fixed" && task.candidate_available === false) return "曾通过验收，候选已变化";
  return healthLabels[task.status] ?? task.status;
}
export function findingStatus(finding: Finding, task: HealthTask): string {
  if (task.governance?.resolved_ids?.includes(finding.id)) return task.candidate_available === false ? "验收后候选已变化" : "本次已解决";
  if (finding.disposition === "needs_decision") return "待判断";
  return task.governance?.selected_ids?.includes(finding.id) ? "本次目标" : "后续可处理";
}
export const healthApi = {
  config: (signal?: AbortSignal) => harnessRequest<HealthConfig>("/code-health/config", { signal }),
  start: (body: StartHealth) => harnessRequest<HealthTask>("/code-health/tasks", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  }),
  history: (page: number, search: string, status: string, signal?: AbortSignal) =>
    harnessRequest<{ tasks: HealthTask[]; total: number }>(`/tasks?${new URLSearchParams({ page: String(page), search, status, kind: "code_health" })}`, { signal }),
  get: (id: string, signal?: AbortSignal) => harnessRequest<HealthTask>(`/tasks/${encodeURIComponent(id)}`, { signal }),
  log: (id: string, reference: string, signal?: AbortSignal) => harnessRequest<{ text: string; truncated: boolean }>(
    `/tasks/${encodeURIComponent(id)}/check-log?${new URLSearchParams({ reference })}`, { signal }),
  patchUrl: (id: string, attempt: number) => `/api/harness/tasks/${encodeURIComponent(id)}/attempts/${attempt}/patch`,
};
