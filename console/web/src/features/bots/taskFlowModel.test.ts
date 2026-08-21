import { describe, expect, it } from "vitest";
import type { BotTask, TaskFlowTransition } from "../../types";
import { buildFlowRows, groupTasks } from "./taskFlowModel";

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
});
