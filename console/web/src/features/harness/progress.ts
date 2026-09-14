import { ACTIVE, type RepairProgress, type RepairTask } from "./api";

export function heartbeatStatus(task: RepairTask, now: number): { label: string; stale: boolean } {
  if (task.heartbeat_at == null) return { label: "最近心跳：尚未记录", stale: false };
  const seconds = Math.max(0, Math.floor(now / 1000 - task.heartbeat_at));
  return { label: `最近心跳：${seconds} 秒前`, stale: ACTIVE.includes(task.status) && seconds > 30 };
}

export function progressSourceLabel(source: NonNullable<RepairProgress["source"]>): string {
  if (source.kind === "prepare") return source.number == null ? "复现准备" : `准备第 ${source.number} 版`;
  return `第 ${source.number} 次${source.kind === "coding" ? "修复" : "审核"}`;
}

export function repairRoundLabel(task: RepairTask): string | null {
  if (["prepare_reproducer", "auto_correcting"].includes(task.stage)) {
    const revisions = task.preparation_revisions ?? [];
    const revision = revisions[revisions.length - 1]?.revision;
    return revision == null ? null : `准备第 ${revision} 版`;
  }
  return task.current_attempt == null ? null : `第 ${task.current_attempt} 次修复`;
}
