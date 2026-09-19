import { harnessRequest } from "./api";
import type { RepairTask } from "./api";

export type GovernanceStop = { mode: "time"; seconds: number } | { mode: "findings"; count: number };
export interface GovernanceOptions {
  model: string; reasoning_effort: string; max_attempts: number; stop_condition: GovernanceStop;
}
export interface GovernanceRun {
  run_id: string; options: GovernanceOptions; status: string; stop_reason: string; message?: string;
  current_task_id: string | null; sequence: number; found_count: number; merged_count: number;
  elapsed_seconds: number; created_at: number; updated_at: number;
  tasks: Array<Pick<RepairTask, "task_id" | "status" | "stage" | "base_commit" | "governance_summary" | "delivery"> & {
    governance_sequence: number; elapsed_seconds?: number; message?: string;
  }>;
}
export const RUN_LABELS: Record<string, string> = {
  running: "执行中", waiting_delivery: "等待当前 PR 合并", cancel_requested: "取消收尾中",
  completed: "已结束", blocked: "已停止，待处理", cancelled: "已取消",
};
export const runActive = (run: GovernanceRun) => ["running", "waiting_delivery", "cancel_requested"].includes(run.status);
export function stopLabel(stop: GovernanceStop) {
  return stop.mode === "time" ? `累计执行 ${Number((stop.seconds / 3600).toFixed(2))} 小时` : `发现 ${stop.count} 个问题`;
}

export interface GovernanceFinding {
  id: string; summary: string; impact: string; principle_refs: string[];
  evidence: Array<{ path: string; excerpt: string }>;
  affected_paths: string[]; acceptance_criteria: string[]; disposition: "automatic" | "needs_decision";
}
export interface GovernanceReport {
  decision: string; summary: string; findings: GovernanceFinding[]; selected_finding_id: string;
  inspected_paths: string[]; uninspected: string[]; unresolved: string[]; inventory_count: number;
  inspection_complete: boolean; evidence_receipts: Array<{ path: string; sha256: string; line: number }>;
}
export interface GovernanceSchedule {
  enabled: boolean; interval_hours: number;
  options: GovernanceOptions | null;
  last_run?: { status: string; run_id?: string; at: number; message?: string; reason?: string };
  last_error?: string; unit?: string; timer?: Record<string, string>;
}
export const governanceApi = {
  start: (value: GovernanceOptions & { request_id: string }) => harnessRequest<GovernanceRun>("/code-health/runs", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(value) }),
  runs: (page: number, search: string, status: string, signal?: AbortSignal) =>
    harnessRequest<{ runs: GovernanceRun[]; total: number }>(`/code-health/runs?${new URLSearchParams({ page: String(page), search, status })}`, { signal, cache: "no-store" }),
  run: (id: string, signal?: AbortSignal) => harnessRequest<GovernanceRun>(`/code-health/runs/${encodeURIComponent(id)}`, { signal, cache: "no-store" }),
  action: (id: string, action: "cancel" | "resume") => harnessRequest<GovernanceRun>(`/code-health/runs/${encodeURIComponent(id)}/${action}`, { method: "POST" }),
  config: (signal?: AbortSignal) => harnessRequest<{ default_model: string }>("/code-health/config", { signal }),
  report: (id: string, signal?: AbortSignal) => harnessRequest<{ state: string; base_commit: string; report: GovernanceReport | null }>(
    `/tasks/${encodeURIComponent(id)}/governance`, { signal, cache: "no-store" }),
  schedule: (signal?: AbortSignal) => harnessRequest<GovernanceSchedule>("/code-health/schedule", { signal, cache: "no-store" }),
  saveSchedule: (value: Pick<GovernanceSchedule, "enabled" | "interval_hours" | "options">) =>
    harnessRequest<GovernanceSchedule>("/code-health/schedule", { method: "PUT",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(value) }),
};
