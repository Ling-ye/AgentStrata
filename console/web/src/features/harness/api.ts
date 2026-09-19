export type SourceKind = "evaluation" | "robot_task";
export interface RepairFeedback { repair_hint?: string; expected_behavior?: string }
export interface SourceWarning { code: string; message: string }
export interface CaseInstance { case_instance_id: string; evaluation_id: string; case_id: string; case_ref: string; target_id: string; trial_id: string; attempt: number; outcome: string }
export interface SourcePreview {
  repetitions?: number;
  kind: SourceKind; bot_id: string; evaluation_id?: string; run_id?: string;
  status?: string; revision?: string; blockers: string[]; case_instance?: CaseInstance;
  warnings?: SourceWarning[];
  evidence?: Record<string, unknown>; history: RepairTask[];
}
export interface Verification {
  evaluation_id: string; kind?: string; complete: boolean; case_ids: string[];
  passed_cases?: string[]; failed_cases?: string[]; test_sha256?: string;
}
export interface Review {
  decision?: "approved" | "rejected" | "inconclusive"; problem?: string; reason?: string; evidence_refs?: string[];
}
export interface Delivery {
  checks?: Array<{ name: string; status: string; conclusion?: string; url?: string }>;
  state: string; repository: string; base_branch: string; base_sha: string;
  commit_sha?: string; merge_sha?: string; branch?: string; pr_url?: string; pr_number?: number;
  auto_merge?: boolean; message?: string; error_code?: string;
}
export interface RepairTask {
  governance_summary?: { summary: string; topic?: string; inspected_count: number; inventory_count: number; inspection_complete: boolean; finding_count: number; needs_decision_count: number };
  delivery?: Delivery;
  cleanup?: { local?: string; remote?: string; error?: string };
  archive?: { digest: string; path: string; head: string };

  pipeline_version?: number;
  archived?: boolean;
  continued_from?: string;
  next_action?: string;
  preparation_revisions?: Array<{ revision: number; status: string; error?: { code: string; message: string } }>;
  acceptance?: { original: string; items: Array<{ id: string; text: string }> };
  acceptance_coverage?: Record<string, { checks: string[]; passed: boolean }>;
  verification_gaps?: Array<{ requirement: string; code: string; message: string }>;
  stop_reason?: string;
  remaining_seconds?: number;
  candidate_checkpoint?: { number: number; candidate_digest: string; changed_files: string[] };
  hypothesis?: { reason: string; expected_behavior: string; evidence_refs: string[] };
  verification_plan?: { real_agent: boolean; repetitions: number; primary_checks: string[]; checks: string[]; snapshot_id: string };
  planned_agent_trials?: number;
  commit_checks?: Array<{ label: string; exit_code: number; output: string }>;
  task_id: string; status: string; stage: string; base_commit: string; created_at: number; updated_at: number;
  source: { kind?: SourceKind | "code_health"; evaluation_id?: string; run_id?: string; bot_id?: string;
    case_id?: string; case_ref?: string; target_id?: string; case_ids?: string[]; case_instance_id?: string; trial_id?: string; attempt?: number;
    blockers?: string[]; test_sha256?: string; test_relative_path?: string; regression_id?: string; feedback?: RepairFeedback;
    warnings?: SourceWarning[];
    diagnosis?: { reason: string; expected_behavior?: string } };
  review_and_commit?: boolean; uncommitted?: boolean | null; commit_state?: string; commit_in_main?: boolean | null;
  local_commit?: { sha: string; branch: string; paths: string[]; message: string };
  regression?: { kind: string; id: string; path?: string; case_ref?: string };
  message?: string; branch?: string; worktree?: string; verified_digest?: string; verified_at?: number;
  candidate_available?: boolean; elapsed_seconds?: number; error_code?: string; current_evaluation_id?: string;
  heartbeat_at?: number; current_attempt?: number;
  options: { model: string; max_attempts: number; reasoning_effort: string; timeout_seconds: number };
  evaluations?: Record<string, Verification>;
  attempts?: Array<{ number: number; status: string; changed_files?: string[]; error?: string; checks?: string[];
    review?: Review; repository_regressions?: { passed_cases: string[]; failed_cases: string[] }; patch_sha256?: string; coding?: { events: Array<Record<string, unknown>> }; regressions?: string[]; verification?: Verification }>;
}
export interface RepairProgress {
  state: "ready" | "empty" | "partial";
  source: { id: string; kind: "prepare" | "coding" | "review" | "audit"; label?: string; number: number | null; current: boolean } | null;
  updated_at: number | null;
  events: Array<{ id: string; type: "agent_message" | "command_execution"; text?: string;
    command?: string; aggregated_output?: string; exit_code?: number | null; truncated: boolean }>;
  truncated: boolean;
  message: string | null;
}
export type ProgressTask = Pick<RepairTask, "task_id" | "status" | "stage" | "elapsed_seconds" |
  "heartbeat_at" | "current_attempt" | "preparation_revisions" | "next_action" | "local_commit" | "commit_in_main" | "commit_state" | "delivery" | "cleanup" | "archive" |
  "pipeline_version" | "verification_gaps" | "acceptance_coverage" | "stop_reason" | "remaining_seconds" | "candidate_checkpoint"> & {
  options: { model: string; max_attempts: number; reasoning_effort: string; timeout_seconds?: number };
  source?: { kind?: SourceKind | "code_health" };
  governance_summary?: RepairTask["governance_summary"];
};
export const ACTIVE = ["queued", "running", "cancel_requested"];
export const REPAIR_LABELS: Record<string, string> = {
  queued: "等待启动", running: "执行中", cancel_requested: "正在取消", fixed: "修复验收通过",
  needs_review: "局部候选待审阅", no_changes: "无需改动",
  waiting_input: "等待原图", not_reproduced: "当前未复现", failed: "修复未通过", blocked: "受阻", cancelled: "已取消", interrupted: "已中断",
};
export const ATTEMPT_LABELS: Record<string, string> = {
  coding: "生成候选", verifying: "复测中", reviewing: "AI 审核中", committing: "本地提交中", review_rejected: "审核认为未修复", review_inconclusive: "审核未能确认", accepted: "验收通过", rejected: "验收未通过",
  needs_review: "局部验证通过", blocked: "需要补充条件",
  coding_failed: "生成失败", interrupted: "已中断",
};
export function stageLabel(stage: string): string {
  const revised = /^(repository-verify|verify|confirm)-(\d+)-r(\d+)$/.exec(stage);
  if (revised) return `第 ${revised[2]} 轮${revised[1] === "repository-verify" ? "仓库回归" : revised[1] === "confirm" ? "独立确认" : "复测"}（方案第 ${revised[3]} 代）`;
  const baseline = /^(reproduce|baseline)-r(\d+)$/.exec(stage);
  if (baseline) return `${baseline[1] === "reproduce" ? "确认原问题" : "建立回归基线"}（方案第 ${baseline[2]} 代）`;
  if (stage.startsWith("repository-verify-")) return `第 ${stage.slice(18)} 轮仓库回归`;
  if (stage.startsWith("confirm-")) return `第 ${stage.slice(8)} 轮独立确认`;
  if (stage.startsWith("verify-")) return `第 ${stage.slice(7)} 轮复测`;
  return ({ auto_correcting: "自动修正复现方案", waiting_image: "等待原图", queued: "等待启动", self_check: "来源自检", prepare_reproducer: "建立复现测试",
    environment: "准备独立依赖环境", snapshot: "冻结源码", definition: "冻结验证草案", regressions: "仓库回归", main: "主 Agent 安排任务", plan: "根因与修复计划", test: "独立验证设计", verify: "候选验收",
    review: "AI 审核", commit: "本地提交", reproduce: "确认当前问题", baseline: "建立回归基线", repository_baseline: "仓库回归基线", coding: "生成候选", done: "完成" } as Record<string, string>)[stage] ?? stage;
}
export function sourceLabel(task: RepairTask): string {
  if (task.source.kind === "code_health") return `代码治理${task.governance_summary?.topic ? " · " + task.governance_summary.topic : ""}`;
  return task.source.kind === "robot_task" ? `机器人任务 ${task.source.run_id}` : `测评 ${task.source.evaluation_id} · ${task.source.case_id}`;
}
export type StartRepair = { source_kind: SourceKind | "code_health"; case_instance_id?: string;
  bot_id?: string; run_id?: string; feedback?: RepairFeedback; request_id: string; model: string; reasoning_effort: string; max_attempts: number; timeout_seconds: number };
export async function harnessRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api/harness${path}`, init);
  const value = await response.json();
  if (!response.ok) throw new Error(typeof value.detail === "string" ? value.detail : value.detail?.message || "Harness 暂不可用");
  return value as T;
}
const request = harnessRequest;
const post = (body: unknown): RequestInit => ({ method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
export const harnessApi = {
  load: (kind: SourceKind, sourceId: string, botId: string) => request<SourcePreview>("/sources/load", post({ kind, source_id: sourceId, bot_id: botId })),
  history: (page: number, search: string, status: string, signal?: AbortSignal, kind = "") => request<{ tasks: RepairTask[]; total: number }>(`/tasks?${new URLSearchParams({ page: String(page), search, status, ...(kind ? { kind } : {}) })}`, { signal }),
  get: (taskId: string, signal?: AbortSignal) => request<RepairTask>(`/tasks/${encodeURIComponent(taskId)}`, { signal }),
  summary: (taskId: string, signal?: AbortSignal) => request<RepairTask>(`/tasks/${encodeURIComponent(taskId)}?summary=true`, { signal, cache: "no-store" }),
  progress: (taskId: string, signal?: AbortSignal) => request<RepairProgress>(`/tasks/${encodeURIComponent(taskId)}/progress`, { signal, cache: "no-store" }),
  evidence: (taskId: string, signal?: AbortSignal) => request<Record<string, unknown>>(`/tasks/${encodeURIComponent(taskId)}/evidence`, { signal, cache: "no-store" }),
  start: (body: StartRepair) => request<RepairTask>("/tasks", post(body)),
  image: (taskId: string, file: File) => request<RepairTask>(`/tasks/${encodeURIComponent(taskId)}/image`, { method: "POST", body: file }),
  action: (taskId: string, action: "cancel" | "resume" | "retry-delivery" | "retry-cleanup" | "continue") => request<RepairTask>(`/tasks/${encodeURIComponent(taskId)}/${action}`, { method: "POST" }),
};

export function repairStatusLabel(task: ProgressTask): string {
  if (task.source?.kind === "code_health" && task.status === "fixed") return "治理验收通过";
  if (task.source?.kind === "code_health" && task.status === "needs_review") return "治理待判断";
  if (task.next_action === "technical_failure") return "技术失败";
  if (task.status === "running" && task.stage === "auto_correcting") return "自动修正中";
  if (task.local_commit) return task.commit_in_main === true ? "已进入本地 main" : "已本地提交";
  if (task.commit_state === "unconfirmed") return "本地提交待核验";
  return REPAIR_LABELS[task.status] ?? task.status;
}

export function deliveryLabel(state: string): string {
  return ({ pending: "等待交付", committed: "已提交任务分支", pushed: "已推送", pr_open: "PR 已创建",
    waiting_checks: "等待 CI 与自动合并", checks_failed: "CI 未通过", updating: "同步主干并复验", retryable: "交付等待重试",
    blocked: "交付受阻", merged: "已合并到远端 main", closed: "PR 已关闭", cancelled: "交付已取消",
    cancel_pending: "正在停止交付", paused: "自动合并已暂停", no_changes: "无可交付成果" } as Record<string, string>)[state] ?? state;
}
export function deliveryActive(task?: { status: string; delivery?: Delivery }): boolean {
  return !!task && (ACTIVE.includes(task.status) || ["pending", "committed", "pushed", "pr_open", "waiting_checks", "checks_failed", "updating", "retryable", "cancel_pending"].includes(task.delivery?.state ?? ""));
}
