import type { BotTask, TaskFlowTransition } from "../../types";

export type FlowRow =
  | { type: "single"; transition: TaskFlowTransition }
  | { type: "bundle"; id: string; transitions: TaskFlowTransition[] };

export function isLiveTask(status: string) {
  return ["running", "delegated", "queued", "cancel_requested"].includes(status);
}

export function taskStatusLabel(status: string) {
  return {
    running: "运行中",
    delegated: "后台继续",
    queued: "排队中",
    succeeded: "已完成",
    failed: "失败",
    skipped: "已跳过",
  }[status] ?? (status || "未知");
}

export function groupTasks(tasks: BotTask[]) {
  return [
    {
      key: "active",
      label: "正在处理",
      tasks: tasks.filter((task) => isLiveTask(task.status)),
    },
    {
      key: "attention",
      label: "需要关注",
      tasks: tasks.filter((task) => ["failed", "error", "cancelled"].includes(task.status)),
    },
    {
      key: "recent",
      label: "最近完成",
      tasks: tasks.filter(
        (task) => !isLiveTask(task.status) && !["failed", "error", "cancelled"].includes(task.status),
      ),
    },
  ];
}

export function buildFlowRows(transitions: TaskFlowTransition[]): FlowRow[] {
  const rows: FlowRow[] = [];
  let capability: TaskFlowTransition[] = [];
  const flush = () => {
    if (capability.length === 1) {
      rows.push({ type: "single", transition: capability[0] });
    } else if (capability.length > 1) {
      rows.push({ type: "bundle", id: capability[0].id, transitions: capability });
    }
    capability = [];
  };
  transitions.forEach((transition) => {
    if (
      transition.source_layer === "capability"
      || transition.target_layer === "capability"
    ) {
      capability.push(transition);
      return;
    }
    flush();
    rows.push({ type: "single", transition });
  });
  flush();
  return rows;
}
