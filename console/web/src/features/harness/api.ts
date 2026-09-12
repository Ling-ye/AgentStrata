export type SourceKind = "evaluation" | "robot_task";
export interface RepairFeedback { repair_hint?: string; expected_behavior?: string }
export interface CaseInstance { case_instance_id: string; evaluation_id: string; case_id: string; case_ref: string; target_id: string; trial_id: string; attempt: number; outcome: string }
export interface SourcePreview {
  kind: SourceKind; bot_id: string; evaluation_id?: string; run_id?: string;
  status?: string; revision?: string; blockers: string[]; case_instance?: CaseInstance;
  evidence?: Record<string, unknown>; history: RepairTask[];
}
export interface Verification {
  evaluation_id: string; kind?: string; complete: boolean; case_ids: string[];
  passed_cases?: string[]; failed_cases?: string[]; test_sha256?: string;
}
export interface Review {
  decision?: "approved" | "rejected" | "inconclusive"; problem?: string; reason?: string; evidence_refs?: string[];
}
export interface RepairTask {
  task_id: string; status: string; stage: string; base_commit: string; created_at: number; updated_at: number;
  source: { kind?: SourceKind; evaluation_id?: string; run_id?: string; bot_id: string;
    case_id: string; case_ref?: string; target_id: string; case_ids: string[]; case_instance_id?: string; trial_id?: string; attempt?: number;
    blockers?: string[]; test_sha256?: string; test_relative_path?: string; regression_id?: string; feedback?: RepairFeedback;
    diagnosis?: { reason: string; expected_behavior?: string } };
  review_and_commit?: boolean; uncommitted?: boolean | null; commit_state?: string; commit_in_main?: boolean | null;
  local_commit?: { sha: string; branch: string; paths: string[]; message: string };
  regression?: { kind: string; id: string; path?: string; case_ref?: string };
  message?: string; branch?: string; worktree?: string; verified_digest?: string; verified_at?: number;
  candidate_available?: boolean; elapsed_seconds?: number; error_code?: string; current_evaluation_id?: string;
  options: { model: string; max_attempts: number; reasoning_effort: string; timeout_seconds: number };
  evaluations?: Record<string, Verification>;
  attempts?: Array<{ number: number; status: string; changed_files?: string[]; error?: string; checks?: string[];
    review?: Review; repository_regressions?: { passed_cases: string[]; failed_cases: string[] }; patch_sha256?: string; coding?: { events: Array<Record<string, unknown>> }; regressions?: string[]; verification?: Verification }>;
}
export const ACTIVE = ["queued", "running", "cancel_requested"];
export const REPAIR_LABELS: Record<string, string> = {
  queued: "等待启动", running: "执行中", cancel_requested: "正在取消", fixed: "已修复 · 待合入",
  not_reproduced: "当前未复现", failed: "修复未通过", blocked: "受阻", cancelled: "已取消", interrupted: "已中断",
};
export const ATTEMPT_LABELS: Record<string, string> = {
  coding: "生成候选", verifying: "复测中", reviewing: "AI 审核中", committing: "本地提交中", review_rejected: "审核认为未修复", review_inconclusive: "审核未能确认", accepted: "验收通过", rejected: "验收未通过",
  coding_failed: "生成失败", interrupted: "已中断",
};
export function stageLabel(stage: string): string {
  if (stage.startsWith("verify-")) return `第 ${stage.slice(7)} 轮复测`;
  return ({ queued: "等待启动", self_check: "来源自检", prepare_reproducer: "建立复现测试",
    review: "AI 审核", commit: "本地提交", reproduce: "确认当前问题", baseline: "建立回归基线", coding: "生成候选", done: "完成" } as Record<string, string>)[stage] ?? stage;
}
export function sourceLabel(task: RepairTask): string {
  return task.source.kind === "robot_task" ? `机器人任务 ${task.source.run_id}` : `测评 ${task.source.evaluation_id} · ${task.source.case_id}`;
}
export type StartRepair = { source_kind: SourceKind; case_instance_id?: string;
  bot_id?: string; run_id?: string; feedback?: RepairFeedback; review_and_commit?: boolean; request_id: string; model: string; reasoning_effort: string; max_attempts: number; timeout_seconds: number };
async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api/harness${path}`, init);
  const value = await response.json();
  if (!response.ok) throw new Error(typeof value.detail === "string" ? value.detail : value.detail?.message || "Harness 暂不可用");
  return value as T;
}
const post = (body: unknown): RequestInit => ({ method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
export const harnessApi = {
  load: (kind: SourceKind, sourceId: string, botId: string) => request<SourcePreview>("/sources/load", post({ kind, source_id: sourceId, bot_id: botId })),
  history: (page: number, search: string, status: string, signal?: AbortSignal) => request<{ tasks: RepairTask[]; total: number }>(`/tasks?${new URLSearchParams({ page: String(page), search, status })}`, { signal }),
  get: (taskId: string, signal?: AbortSignal) => request<RepairTask>(`/tasks/${encodeURIComponent(taskId)}`, { signal }),
  evidence: (taskId: string) => request<Record<string, unknown>>(`/tasks/${encodeURIComponent(taskId)}/evidence`),
  start: (body: StartRepair) => request<RepairTask>("/tasks", post(body)),
  action: (taskId: string, action: "cancel" | "resume") => request<RepairTask>(`/tasks/${encodeURIComponent(taskId)}/${action}`, { method: "POST" }),
};

export function repairStatusLabel(task: RepairTask): string {
  if (task.local_commit) return task.commit_in_main === true ? "已进入本地 main" : "已本地提交";
  if (task.commit_state === "unconfirmed") return "本地提交待核验";
  return REPAIR_LABELS[task.status] ?? task.status;
}
