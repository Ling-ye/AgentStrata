import { harnessRequest } from "./api";

export interface FlowStep {
  id: string; title: string; parent_id: string; phase: string; status: string;
  input_summary: string; conclusion: string; started_at?: number | null; finished_at?: number | null;
  attempt?: number; revision?: number; generation?: number; source_id?: string; input_truncated?: boolean;
}
export interface FlowGroup { id: string; title: string; parent_id: string | null; started_at?: number }
export interface RepairFlow { task_id: string; groups: FlowGroup[]; steps: FlowStep[]; current_step_id?: string; default_step_id?: string }
export interface StepDetail {
  id: string; status: string; input: unknown; conclusion: string; evidence: unknown; result: unknown;
  detail_state: string; input_truncated: boolean; traces: Array<{ trace_ref: string; capture_state: string }>;
}
export interface CommandSource { id: string; kind: string; number: number | null; current: boolean; label?: string }
export interface CommandEvent { id: string; command: string; aggregated_output?: string; exit_code?: number | null; truncated: boolean }
export interface CommandPage {
  source: CommandSource | null; sources: CommandSource[]; events: CommandEvent[];
  next_cursor: string | null; has_more: boolean; truncated: boolean; message: string | null;
}
export const flowApi = {
  flow: (task: string, signal?: AbortSignal) => harnessRequest<RepairFlow>(`/tasks/${encodeURIComponent(task)}/flow`, { signal, cache: "no-store" }),
  step: (task: string, id: string, signal?: AbortSignal) => harnessRequest<StepDetail>(`/tasks/${encodeURIComponent(task)}/flow/steps/${encodeURIComponent(id)}`, { signal, cache: "no-store" }),
  commands: (task: string, source = "", cursor = "", signal?: AbortSignal) => harnessRequest<CommandPage>(`/tasks/${encodeURIComponent(task)}/commands?${new URLSearchParams({ source_id: source, cursor })}`, { signal, cache: "no-store" }),
};
export const stepLabels: Record<string, string> = { recorded: "回执已记录", running: "执行中", completed: "已完成", failed: "未通过", cancelled: "已取消", interrupted: "未确认完成", blocked: "受阻" };
export function descendants(flow: RepairFlow, id: string): FlowStep[] {
  const ids = new Set([id]);
  for (let size = 0; size !== ids.size;) {
    size = ids.size;
    flow.groups.forEach(g => { if (g.parent_id && ids.has(g.parent_id)) ids.add(g.id); });
  }
  return flow.steps.filter(s => ids.has(s.parent_id));
}
export function flowChildren(flow: RepairFlow, id: string): Array<{ kind: "group" | "step"; id: string }> {
  const phases = ["source", "snapshot", "main", "plan", "test", "prepare", "prepare_trial", "prepare_review", "prepare_result", "reproduce", "baseline", "repository_baseline", "coding", "verify", "repository", "confirm", "review", "acceptance"];
  const items = [
    ...flow.groups.filter(g => g.parent_id === id && descendants(flow, g.id).length).map(g => {
      const steps = descendants(flow, g.id);
      return { kind: "group" as const, id: g.id, time: g.started_at ?? steps.find(s => s.started_at)?.started_at, rank: Number(g.id.split("-").slice(-1)[0]) || 0 };
    }),
    ...flow.steps.filter(s => s.parent_id === id).map(s => ({ kind: "step" as const, id: s.id,
      time: s.phase === "prepare_result" ? s.finished_at : s.started_at,
      rank: phases.findIndex(p => s.phase === p || s.phase.startsWith(p + "-")) })),
  ];
  return items.sort((a, b) => a.time && b.time ? a.time - b.time : a.rank - b.rank);
}
export function initialExpansion(flow: RepairFlow): Record<string, boolean> {
  const result: Record<string, boolean> = {};
  const step = flow.steps.find(s => s.id === flow.default_step_id);
  if (!step) return result;
  result[step.id] = true;
  let parent: string | null | undefined = step.parent_id;
  while (parent && !result[parent]) {
    result[parent] = true;
    parent = flow.groups.find(group => group.id === parent)?.parent_id;
  }
  return result;
}
export function visibleCommands(events: CommandEvent[], failures: boolean): CommandEvent[] {
  return events.filter(e => !failures || e.exit_code != null && e.exit_code !== 0);
}
export function commandSourceLabel(source: CommandSource): string {
  if (source.label) return source.label;
  if (source.id.startsWith("prepare-review-")) return `第 ${source.number} 版 · 方案审核`;
  if (source.kind === "prepare") return source.number ? `第 ${source.number} 版 · 复现方案` : "复现准备";
  return `${source.number ? `第 ${source.number} 轮 · ` : ""}${source.kind === "review" ? "修复审核" : source.kind === "coding" ? "生成候选" : "执行日志"}`;
}
