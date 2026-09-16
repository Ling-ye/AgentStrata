import { ACTIVE, type RepairProgress, type ProgressTask } from "./api";

export function heartbeatStatus(task: Pick<ProgressTask, "heartbeat_at" | "status">, now: number): { label: string; stale: boolean } {
  if (task.heartbeat_at == null) return { label: "最近心跳：尚未记录", stale: false };
  const seconds = Math.max(0, Math.floor(now / 1000 - task.heartbeat_at));
  return { label: `最近心跳：${seconds} 秒前`, stale: ACTIVE.includes(task.status) && seconds > 30 };
}

export function progressSourceLabel(source: NonNullable<RepairProgress["source"]>): string {
  if (source.label) return source.label;
  if (source.kind === "audit") return "代码只读巡检";
  if (source.kind === "prepare") return source.number == null ? "复现准备" : `准备第 ${source.number} 版`;
  return `第 ${source.number} 次${source.kind === "coding" ? "修复" : "审核"}`;
}

export function repairRoundLabel(task: Pick<ProgressTask, "stage" | "preparation_revisions" | "current_attempt">): string | null {
  if (["prepare_reproducer", "auto_correcting"].includes(task.stage)) {
    const revisions = task.preparation_revisions ?? [];
    const revision = revisions[revisions.length - 1]?.revision;
    return revision == null ? null : `准备第 ${revision} 版`;
  }
  return task.current_attempt == null ? null : `第 ${task.current_attempt} 次修复`;
}

export function budgetLabel(task: ProgressTask): string {
  const elapsed = task.elapsed_seconds == null ? "用时尚未记录" : `已用 ${Math.round(task.elapsed_seconds)} 秒`;
  const budget = task.options.budget;
  if (budget?.mode === "discovered_groups") {
    const counts = task.governance_summary;
    return `已发现 ${counts?.discovered_groups ?? 0} 组（目标 ${budget.count} 组） · 本轮选中 ${counts?.selected_groups ?? 0} 组 · 已验收 ${counts?.accepted_groups ?? 0} 组 · ${elapsed}`;
  }
  if (budget?.mode === "fixed_groups") {
    return `已验收 ${task.governance_summary?.accepted_groups ?? 0}/${budget.count} 组 · ${elapsed}`;
  }
  const seconds = budget?.mode === "time" ? budget.seconds : task.options.timeout_seconds;
  return seconds == null ? elapsed : `${elapsed} / 总时限 ${seconds} 秒`;
}
