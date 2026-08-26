import type { BotTask, TaskFlowTransition, TasksResponse } from "../../types";

const ACTIVE_TASK_STATUSES = new Set(["queued", "running", "delegated", "cancel_requested"]);
const TERMINAL_TASK_STATUSES = new Set(["succeeded", "failed", "error", "cancelled"]);

export function updateExpandedStepIds(
  current: ReadonlySet<string>,
  stepId: string,
  isOpen: boolean,
) {
  const next = new Set(current);
  if (isOpen) next.add(stepId);
  else next.delete(stepId);
  return next;
}

export function taskDeleteAvailability(status: string) {
  if (ACTIVE_TASK_STATUSES.has(status)) {
    return {
      allowed: false,
      reason: "任务仍在运行；删除记录不会取消执行，请等待任务结束。",
    };
  }
  if (!TERMINAL_TASK_STATUSES.has(status)) {
    return {
      allowed: false,
      reason: "任务状态无法确认，已拒绝删除。",
    };
  }
  return { allowed: true, reason: "删除此任务记录" };
}

export function nextTaskIdAfterDelete(tasks: BotTask[], taskId: string) {
  const index = tasks.findIndex((task) => task.task_id === taskId);
  if (index < 0) return tasks[0]?.task_id ?? "";
  return tasks[index + 1]?.task_id ?? tasks[index - 1]?.task_id ?? "";
}

export function withoutTaskRecord(response: TasksResponse, taskId: string): TasksResponse {
  if (!response.tasks.some((task) => task.task_id === taskId)) return response;
  return {
    ...response,
    count: Math.max(0, response.count - 1),
    total_count: Math.max(0, response.total_count - 1),
    tasks: response.tasks.filter((task) => task.task_id !== taskId),
  };
}

export type FlowRow =
  | { type: "single"; transition: TaskFlowTransition }
  | { type: "bundle"; id: string; transitions: TaskFlowTransition[] };

export function isLiveTask(status: string) {
  return ["running", "delegated", "queued", "cancel_requested"].includes(status);
}

export function shouldRefreshTerminalFlow(
  previous: Readonly<{ key: string; status: string }> | null,
  current: Readonly<{ key: string; status: string }> | null,
) {
  return Boolean(
    previous
    && current
    && previous.key === current.key
    && isLiveTask(previous.status)
    && TERMINAL_TASK_STATUSES.has(current.status),
  );
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
