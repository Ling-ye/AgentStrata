import { harnessRequest, HarnessRequestError } from "./api";

export interface ApplicationChoice {
  bot_id: string; display_name: string; runtime_id: "langgraph" | "codex"; agent_profile: string; channels: string[];
}
export interface ActivationTarget { target_id: string; display_name: string; kind: "instance" | "plugin"; bot_id?: string; version?: string }
export interface ImprovementPreview {
  runtime_id: string; target_id: string; case_ids: string[]; evidence?: { run?: { model?: string } };
}
export interface StartImprovement {
  task_type: "integrate" | "improve"; bot_id: string; goal: string; allowed_paths: string[];
  goal_case_ids: string[]; suite_id: string; source_envelope: Record<string, string>;
  profile: string; max_attempts: number; timeout_seconds: number; request_id: string;
}
export interface CheckFact { check_id: string; repetition: number; outcome: string; failure_kind?: string }
export interface ComparisonPhase { candidate_digest?: string; checks?: CheckFact[]; versions?: Record<string, unknown>; run_id?: string }
export interface Comparison {
  task_id: string; attempt?: number; goal: string; runtime_id?: string; candidate_digest?: string;
  changed_files: string[]; phases: Record<"reproduction" | "verification" | "confirmation", ComparisonPhase>;
  accepted_candidate?: Record<string, unknown> | null; review?: { decision?: string; reason?: string };
  verification_gaps: Array<{ code?: string; message?: string; requirement?: string }>;
  plan?: { primary_checks: string[]; protected_checks: string[]; repetitions: number };
}
export interface Activation {
  id: string; kind: string; task_id: string; target_id: string; state: string; desired_version: string;
  running_matches: boolean | null; running_versions?: Record<string, string>; cancel_requested?: boolean;
  message?: string; error?: string; recovery_error?: string; restored?: boolean;
  changed_paths?: string[]; steps?: Array<{ state: string; at: number }>;
}
export type ActivationAction = "prepare" | "activate" | "rollback" | "reconcile" | "cancel";
const post = (body: unknown): RequestInit => ({ method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
const taskPath = (id: string) => `/tasks/${encodeURIComponent(id)}`;
export const improvementApi = {
  applications: (signal?: AbortSignal) => harnessRequest<{ applications: ApplicationChoice[]; activation_targets: ActivationTarget[]; unavailable: Array<{ application: string; message: string }> }>("/applications", { signal }),
  preview: (bot: string, suite: string, signal?: AbortSignal) => harnessRequest<ImprovementPreview>(`/applications/${encodeURIComponent(bot)}/preview?${new URLSearchParams({ suite_id: suite })}`, { signal }),
  start: (body: StartImprovement) => harnessRequest<import("./api").RepairTask>("/improvements", post(body)),
  comparison: (id: string, signal?: AbortSignal) => harnessRequest<Comparison>(`${taskPath(id)}/comparison`, { signal }),
  activation: async (id: string, signal?: AbortSignal): Promise<Activation | null> => {
    try { return await harnessRequest<Activation>(`${taskPath(id)}/activation`, { signal }); }
    catch (error) { if (error instanceof HarnessRequestError && error.code === "activation_missing") return null; throw error; }
  },
  changeActivation: (id: string, action: ActivationAction, target?: string) => harnessRequest<Activation>(`${taskPath(id)}/activation/${action}`, post(target ? { target_id: target } : {})),
};
export const ACTIVATION_PENDING = ["draining", "stopping", "switching", "starting", "restoring", "recovery_required"];
export const activationLabel = (state: string) => ({ prepared: "版本已准备", draining: "等待在途任务收尾", stopping: "停止原版本", switching: "切换版本", starting: "启动并核验", active: "版本已启用", restoring: "恢复原版本", rolled_back: "已撤回", recovery_required: "需要恢复对账", cancelled: "已取消", failed: "启用失败" } as Record<string, string>)[state] ?? state;

export function comparisonRows(value: Comparison) {
  const phases = ["reproduction", "verification", "confirmation"] as const;
  const rows = new Map<string, { key: string; check: string; repetition: number; primary: boolean; facts: Partial<Record<typeof phases[number], CheckFact>> }>();
  for (const phase of phases) for (const fact of value.phases[phase].checks ?? []) {
    const key = JSON.stringify([fact.check_id, fact.repetition]);
    const row = rows.get(key) ?? { key, check: fact.check_id, repetition: fact.repetition, primary: !!value.plan?.primary_checks.includes(fact.check_id), facts: {} };
    row.facts[phase] = fact;
    rows.set(key, row);
  }
  return [...rows.values()];
}
