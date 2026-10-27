import { afterEach, expect, it, vi } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { deliveryActive, type RepairTask } from "./api";
import { ApplicationResult } from "./ApplicationResult";
import { comparisonRows, improvementApi, type Comparison } from "./improvements";

afterEach(() => vi.unstubAllGlobals());
const comparison: Comparison = { task_id: "goal", goal: "Improve action", changed_files: [], verification_gaps: [],
  phases: { reproduction: { checks: [{ check_id: "action", repetition: 1, outcome: "failed", failure_kind: "product" }] }, verification: {}, confirmation: {} } };

it("preserves absent and environment evidence instead of converting it to a pass", () => {
  const rows = comparisonRows({ ...comparison, phases: { ...comparison.phases,
    verification: { checks: [{ check_id: "action", repetition: 1, outcome: "error", failure_kind: "environment" }] } } });
  expect(rows).toHaveLength(1);
  expect(rows[0].facts.reproduction?.outcome).toBe("failed");
  expect(rows[0].facts.verification?.failure_kind).toBe("environment");
  expect(rows[0].facts.confirmation).toBeUndefined();
});

it("only treats a missing activation as unprepared, keeping other errors visible", async () => {
  const fetch = vi.fn().mockResolvedValue({ ok: false, json: async () => ({ detail: { code: "activation_missing", message: "not prepared" } }) });
  vi.stubGlobal("fetch", fetch);
  expect(await improvementApi.activation("goal")).toBeNull();
  fetch.mockResolvedValue({ ok: false, json: async () => ({ detail: { code: "activation_superseded", message: "other task owns instance" } }) });
  await expect(improvementApi.activation("goal")).rejects.toThrow("other task owns instance");
});

it("binds reads to the selected task and sends version changes as explicit writes", async () => {
  const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) });
  vi.stubGlobal("fetch", fetch);
  const signal = new AbortController().signal;
  await improvementApi.comparison("goal /a", signal);
  await improvementApi.changeActivation("goal /a", "activate", "plugin:vtuber:main");
  expect(fetch.mock.calls[0]).toEqual(["/api/harness/tasks/goal%20%2Fa/comparison", { signal }]);
  expect(fetch.mock.calls[1][1].method).toBe("POST");
  expect(JSON.parse(fetch.mock.calls[1][1].body)).toEqual({ target_id: "plugin:vtuber:main" });
});

it("keeps archived tasks read only even when the old status says running", () => {
  const task = { task_id: "goal", status: "running", archived: true, source: { kind: "application", bot_id: "sample" } } as RepairTask;
  expect(deliveryActive(task)).toBe(false);
  const client = new QueryClient();
  client.setQueryData(["harness-comparison", "goal"], comparison);
  const html = renderToStaticMarkup(createElement(QueryClientProvider, { client }, createElement(ApplicationResult, { task })));
  expect(html).toContain("旧协议任务只读归档");
  expect(html).not.toContain("启用已验收版本");
  expect(html).not.toContain("准备候选版本");
});

it("shows accepted evidence separately from an unconfirmed running version", () => {
  const task = { task_id: "goal", status: "fixed", archived: false, candidate_available: true,
    source: { kind: "application", bot_id: "sample" } } as RepairTask;
  const client = new QueryClient();
  client.setQueryData(["harness-comparison", "goal"], { ...comparison, accepted_candidate: { candidate_digest: "hash" } });
  client.setQueryData(["harness-activation", "goal"], { state: "active", desired_version: "hash", target_id: "instance:sample", running_matches: null });
  const html = renderToStaticMarkup(createElement(QueryClientProvider, { client }, createElement(ApplicationResult, { task })));
  expect(html).toContain("已签发独立接受回执");
  expect(html).toContain("暂时无法确认实际运行版本");
  expect(html).toContain("撤回版本");
  expect(html).not.toContain("实际运行身份与本次操作记录一致");
});
