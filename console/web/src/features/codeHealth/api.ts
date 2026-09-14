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
  number: number; group_id?: string; group_attempt?: number; status: string; changed_files?: string[]; error?: string; patch_sha256?: string;
  review?: Review; verification?: { profile: string; passed: boolean; checks: Check[]; accepted?: boolean; retained_failures?: string[] };
  after?: { checks: Check[]; findings: Finding[] };
}
export interface HealthSummary {
  found: number; fixed: number; needs_decision: number; remaining: number;
  coverage: "unknown" | "complete" | "partial"; completed_batches: number; total_batches: number;
}
export interface HealthGroup {
  id: string; key: string; status: string; finding_ids: string[]; attempts: number[];
  reason?: string; stop_reason?: string; depends_on: string[]; checkpoint?: number;
  proof?: { kind: string; sha256?: string; diagnosis?: { reason: string; structural_before?: string } };
}
export interface Checkpoint { number: number; attempt: number; digest: string; changed_files: string[] }
export interface Coverage {
  id: string; area: string; status: string; summary?: string; reason?: string;
  blocks: Array<{ path: string; start_line?: number; end_line?: number; sha256: string; block_sha256?: string }>;
}
export interface HealthTask extends ProgressTask {
  base_commit: string; created_at: number; updated_at: number; message?: string; error_code?: string;
  source: { kind: "code_health"; scope: Scope; snapshot_digest: string };
  branch?: string; worktree?: string; candidate_available?: boolean | null; attempts?: HealthAttempt[];
  check_logs?: Check[]; governance_summary?: HealthSummary; current_group?: string; checkpoint?: Checkpoint;
  checkpoint_available?: boolean; stop_reason?: string;
  governance?: { version?: number; groups?: HealthGroup[]; coverage?: Coverage[]; checkpoints?: Checkpoint[]; before: { checks: Check[]; findings: Finding[] }; findings: Finding[];
    selected_ids?: string[]; resolved_ids?: string[]; audit_summary?: string; inspected_paths?: string[] };
}
export type StartHealth = ProgressTask["options"] & { scope: Scope; request_id: string };
export const healthLabels: Record<string, string> = {
  queued: "等待启动", running: "执行中", cancel_requested: "正在取消", fixed: "已验证，待人工提交",
  not_reproduced: "扫描完成", failed: "候选未通过", blocked: "受阻", cancelled: "已取消", interrupted: "已中断",
};
const ACTIVE_STATUS = ["queued", "running", "cancel_requested"];
export function healthSummary(task: Pick<HealthTask, "governance_summary">): string {
  const s = task.governance_summary;
  return s ? `发现 ${s.found} 项，已修复 ${s.fixed} 项，待判断 ${s.needs_decision} 项，未处理 ${s.remaining} 项` : "发现数量未知";
}
export function healthStatus(task: Pick<HealthTask, "status" | "candidate_available" | "governance_summary" | "stop_reason">): string {
  if (task.status === "fixed" && task.candidate_available === false) return "曾通过验收，候选已变化";
  if (task.status === "cancelled" || task.status === "interrupted") return healthLabels[task.status];
  if (task.stop_reason === "budget_exhausted") return "已达总时限";
  if (task.stop_reason && task.stop_reason !== "completed") return "执行受阻";
  const counts = task.governance_summary;
  if (task.status === "fixed" && task.candidate_available == null && counts?.coverage === "unknown") return "历史验收通过";
  if (counts && !ACTIVE_STATUS.includes(task.status)) {
    if (counts.fixed > 0 && (counts.remaining > 0 || counts.needs_decision > 0 || counts.coverage !== "complete")) return "已修复部分";
    if (counts.needs_decision > 0 && counts.fixed === 0) return "发现问题，待判断";
    if (counts.found === 0 && counts.coverage === "complete") return "本轮未发现问题";
  }
  return healthLabels[task.status] ?? task.status;
}
export function findingStatus(finding: Finding, task: HealthTask): string {
  if (task.governance?.resolved_ids?.includes(finding.id)) {
    if (task.checkpoint) return task.checkpoint_available === false ? "检查点不可读取" : "已保存在检查点";
    return task.candidate_available === false ? "验收后候选已变化" : "本次已解决";
  }
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
  candidateUrl: (id: string) => `/api/harness/tasks/${encodeURIComponent(id)}/candidate-patch`,
  patchUrl: (id: string, attempt: number) => `/api/harness/tasks/${encodeURIComponent(id)}/attempts/${attempt}/patch`,
};
