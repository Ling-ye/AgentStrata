import { describe, expect, it } from "vitest";
import type { BotTask, TaskFlowTransition } from "../../types";
import {
  buildFlowRows,
  groupTasks,
  nextTaskIdAfterDelete,
  shouldRefreshTerminalFlow,
  taskFlowAvailability,
  taskDeleteAvailability,
  updateExpandedStepIds,
  withoutTaskRecord,
} from "./taskFlowModel";

function transition(
  id: string,
  source: TaskFlowTransition["source_layer"],
  target: TaskFlowTransition["target_layer"],
): TaskFlowTransition {
  return {
    id,
    sequence: Number(id.replace(/\D/g, "")) || 1,
    kind: id,
    source_layer: source,
    target_layer: target,
    status: "succeeded",
    evidence_level: "observed",
    title: id,
    summary: "",
    occurred_at: 1,
    duration_ms: null,
    decision: {},
    payload: {},
    evidence: [],
  };
}

function task(task_id: string, status: string): BotTask {
  return {
    task_id,
    status,
    description: task_id,
    progress: "",
    submitter: "",
    asked_at: 1,
    updated_at: 1,
    sort_time: 1,
    job_ids: [],
  };
}

describe("task flow presentation model", () => {
  it("disables the legacy task-flow projection for Gateway instances", () => {
    expect(taskFlowAvailability("gateway")).toEqual({
      available: false,
      code: "gateway_task_flow_unavailable",
      message: "Gateway 的受限任务流投影尚未接入；Console 不会回退展示旧 Relay、cc-connect 或 ACP 任务证据。",
    });
    expect(taskFlowAvailability("legacy")).toEqual({
      available: true,
      code: null,
      message: "",
    });
  });

  it("updates expanded timeline steps without mutating the current selection", () => {
    const current = new Set(["first"]);
    const expanded = updateExpandedStepIds(current, "second", true);
    const repeated = updateExpandedStepIds(expanded, "second", true);
    const collapsed = updateExpandedStepIds(repeated, "first", false);

    expect([...current]).toEqual(["first"]);
    expect([...expanded]).toEqual(["first", "second"]);
    expect([...repeated]).toEqual(["first", "second"]);
    expect([...collapsed]).toEqual(["second"]);
    expect(expanded).not.toBe(current);
    expect(repeated).not.toBe(expanded);
  });

  it("collapses only consecutive capability traffic and preserves layer boundaries", () => {
    const rows = buildFlowRows([
      transition("middleware1", "gateway", "middleware"),
      transition("tool2", "agent", "capability"),
      transition("tool3", "capability", "agent"),
      transition("model4", "agent", "model"),
      transition("delivery5", "agent", "delivery"),
    ]);

    expect(rows.map((row) => row.type)).toEqual(["single", "bundle", "single", "single"]);
    expect(rows[1].type === "bundle" ? rows[1].transitions.map((item) => item.id) : []).toEqual([
      "tool2",
      "tool3",
    ]);
  });

  it("groups active, attention, and completed tasks without changing backend status", () => {
    const groups = groupTasks([
      task("running", "running"),
      task("delegated", "delegated"),
      task("failed", "failed"),
      task("done", "succeeded"),
    ]);

    expect(groups.map((group) => group.tasks.map((item) => item.task_id))).toEqual([
      ["running", "delegated"],
      ["failed"],
      ["done"],
    ]);
  });

  it("refreshes one selected flow only when the same task becomes terminal", () => {
    expect(shouldRefreshTerminalFlow(
      { key: "bot-a:task-1", status: "running" },
      { key: "bot-a:task-1", status: "succeeded" },
    )).toBe(true);
    expect(shouldRefreshTerminalFlow(
      { key: "bot-a:task-1", status: "delegated" },
      { key: "bot-a:task-1", status: "failed" },
    )).toBe(true);
    expect(shouldRefreshTerminalFlow(
      { key: "bot-a:task-1", status: "running" },
      { key: "bot-a:task-1", status: "delegated" },
    )).toBe(false);
    expect(shouldRefreshTerminalFlow(
      { key: "bot-a:task-1", status: "succeeded" },
      { key: "bot-a:task-1", status: "failed" },
    )).toBe(false);
    expect(shouldRefreshTerminalFlow(
      { key: "bot-a:task-1", status: "running" },
      { key: "bot-a:task-1", status: "unknown" },
    )).toBe(false);
    expect(shouldRefreshTerminalFlow(
      { key: "bot-a:task-1", status: "running" },
      { key: "bot-a:task-2", status: "succeeded" },
    )).toBe(false);
  });

  it("exposes deletion only for recognized terminal task records", () => {
    expect(taskDeleteAvailability("succeeded").allowed).toBe(true);
    expect(taskDeleteAvailability("failed").allowed).toBe(true);
    expect(taskDeleteAvailability("running")).toEqual({
      allowed: false,
      reason: "任务仍在运行；删除记录不会取消执行，请等待任务结束。",
    });
    expect(taskDeleteAvailability("unknown").allowed).toBe(false);
  });

  it("selects the next task, then the previous task, after deletion", () => {
    const tasks = [task("first", "succeeded"), task("second", "failed"), task("third", "succeeded")];
    expect(nextTaskIdAfterDelete(tasks, "second")).toBe("third");
    expect(nextTaskIdAfterDelete(tasks, "third")).toBe("second");
    expect(nextTaskIdAfterDelete([tasks[0]], "first")).toBe("");
  });

  it("removes a deleted record from the shared task-list cache", () => {
    const response = {
      instance_id: "bot",
      workspace_root: "/redacted",
      workspace_exists: true,
      task_flow_available: true,
      task_flow_unavailable_reason: null,
      count: 2,
      total_count: 2,
      summary: { active_count: 0, failed_recent_count: 1, last_activity_at: 2 },
      tasks: [task("first", "failed"), task("second", "succeeded")],
    };

    expect(withoutTaskRecord(response, "first")).toMatchObject({
      count: 1,
      total_count: 1,
      tasks: [{ task_id: "second" }],
    });
    expect(withoutTaskRecord(response, "missing")).toBe(response);
  });
});
