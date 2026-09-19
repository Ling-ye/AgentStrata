import { harnessRequest } from "./api";

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
  enabled: boolean; interval_hours: number; repair_hint: string;
  options: { model: string; reasoning_effort: string; max_attempts: number; timeout_seconds: number; single_issue?: boolean } | null;
  last_run?: { status: string; task_id?: string; at: number; message?: string; reason?: string };
  last_error?: string; unit?: string; timer?: Record<string, string>;
}
export const governanceApi = {
  config: (signal?: AbortSignal) => harnessRequest<{ default_model: string }>("/code-health/config", { signal }),
  report: (id: string, signal?: AbortSignal) => harnessRequest<{ state: string; base_commit: string; report: GovernanceReport | null }>(
    `/tasks/${encodeURIComponent(id)}/governance`, { signal, cache: "no-store" }),
  schedule: (signal?: AbortSignal) => harnessRequest<GovernanceSchedule>("/code-health/schedule", { signal, cache: "no-store" }),
  saveSchedule: (value: Pick<GovernanceSchedule, "enabled" | "interval_hours" | "options" | "repair_hint">) =>
    harnessRequest<GovernanceSchedule>("/code-health/schedule", { method: "PUT",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(value) }),
};
