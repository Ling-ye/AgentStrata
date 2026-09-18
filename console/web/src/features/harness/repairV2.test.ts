import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { expect, it } from "vitest";
import { RepairProgress } from "./RepairProgress";
import { REPAIR_LABELS, type ProgressTask } from "./api";

it("shows bounded rounds, saved candidate and unverified requirements without a success claim", () => {
  const task: ProgressTask = {
    task_id: "repair-fixture", pipeline_version: 8, status: "needs_review", stage: "done",
    current_attempt: 2, elapsed_seconds: 100, remaining_seconds: 3500, stop_reason: "acceptance_gap",
    options: { model: "fixture", reasoning_effort: "medium", max_attempts: 3, timeout_seconds: 3600 },
    acceptance_coverage: { expected_behavior: { passed: false, checks: [] } },
    verification_gaps: [{ requirement: "expected_behavior", code: "fixture_missing", message: "真实平台回执未验证" }],
    candidate_checkpoint: { number: 2, candidate_digest: "digest", changed_files: ["product.py"] },
  };
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const html = renderToStaticMarkup(createElement(QueryClientProvider, { client },
    createElement(RepairProgress, { task, summaryOnly: true, refreshTask: async () => {} })));
  expect(html).toContain("第 2/3 轮修复");
  expect(html).toContain(REPAIR_LABELS.needs_review);
  expect(html).toContain("候选已保存");
  expect(html).toContain("真实平台回执未验证");
  expect(html).toContain("acceptance_gap");
  expect(html).not.toContain(REPAIR_LABELS.fixed);
  client.clear();
});
