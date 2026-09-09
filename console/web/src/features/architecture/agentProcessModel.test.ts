import { describe, expect, it } from "vitest";
import { agentProcess, processPreview } from "./agentProcessModel";
import { buildRunView, stepIsOpen, rememberOpenedSteps } from "./workbenchModel";
import type { GatewayObservation } from "./model";
const event = (seq: number, extra: Partial<GatewayObservation>): GatewayObservation => ({
  seq, created_at: seq, kind: "LlmCallStarted", trace_id: "t", span_id: "a", ...extra,
  data: { flow_version: 1, runtime_layer: "agent", ...extra.data },
});
describe("Agent process presentation adapter", () => {
  it("keeps MCP catalog observations separate from actual tool calls", () => {
    for (const [phase, title] of [["initialized", "MCP 服务初始化"], ["list_response_prepared", "已处理 MCP 工具列表请求"], ["failed", "MCP 工具接入失败"]]) {
      const item = event(1, { kind: "ToolCatalogObserved", span_id: "catalog", data: { process_kind: "tool_catalog", catalog_phase: phase, source: "session_gateway" } });
      const model = agentProcess(buildRunView([item]).steps[0]);
      expect(model.title).toBe(title);
      expect(model.panels[0].event?.seq).toBe(1);
      expect(model.panels).toHaveLength(1);
      expect(model.title).not.toContain("工具调用");
    }
  });
  it("uses real rounds and direct effective input instead of a backend-specific component", () => {
    const view = buildRunView([
      event(1, { phase: "start", data: { process_kind: "model_call", iteration: 1, context_snapshot_id: "ctx" } }),
      event(2, { kind: "ContextSnapshotPrepared", data: { snapshot_id: "ctx" } }),
      event(3, { kind: "LlmCallFinished", phase: "finish", data: { process_kind: "model_call", iteration: 1 } }),
    ]);
    const model = agentProcess(view.steps[0]);
    expect(model.title).toContain("第 2 轮");
    expect(model.panels[0].event?.seq).toBe(2);
    expect(model.panels[0].select?.({ effective_messages: [{ role: "tool", content: "result" }] })).toEqual([{ role: "tool", content: "result" }]);
  });
  it("keeps adapter execution distinct from internal model rounds", () => {
    const model = agentProcess(buildRunView([event(1, { data: { process_kind: "backend_execution", iteration: 0 } })]).steps[0]);
    expect(model.title).toContain("后端执行");
    expect(model.title).not.toContain("第 1 轮");
  });
  it("merges message revisions by exact trace/span and preserves reading overrides", () => {
    const first = event(1, { kind: "AgentMessageObserved", phase: "update", data: { process_kind: "message", revision: 1 } });
    const next = event(2, { ...first, seq: 2, data: { process_kind: "message", revision: 2 } });
    const finish = event(3, { ...first, seq: 3, phase: "finish", data: { process_kind: "message", revision: 3 } });
    const before = buildRunView([first]).steps[0];
    const after = buildRunView([first, next, finish, event(4, { ...first, seq: 4, data: { process_kind: "message", revision: 1 } })]).steps[0];
    expect(after.key).toBe(before.key);
    expect(after.event.seq).toBe(3);
    expect(after.update?.data?.revision).toBe(2);
    expect(stepIsOpen(after, { [before.key]: true }, true)).toBe(true);
    expect(buildRunView([first, event(2, { ...first, seq: 2, trace_id: "other" })]).steps).toHaveLength(2);
  });
  it("retains automatic live expansion on completion and respects manual collapse", () => {
    const step = buildRunView([event(1, { kind: "AgentMessageObserved", phase: "update", status: "running" })]).steps[0];
    const remembered = rememberOpenedSteps([step], {}, false);
    const completed = { ...step, event: { ...step.event, status: "succeeded" } };
    expect(stepIsOpen(completed, remembered, true)).toBe(true);
    const collapsed = { [step.key]: false };
    expect(rememberOpenedSteps([step], collapsed, false)).toBe(collapsed);
    expect(stepIsOpen(completed, collapsed, true)).toBe(false);
  });
  it.each(["command", "file_change", "mcp_tool", "web_search", "plan", "reasoning", "subagent"])("recognizes %s without raw provider frames", (kind) => {
    const model = agentProcess(buildRunView([event(1, { kind: "SpanFinished", phase: "finish", data: { process_kind: kind } })]).steps[0]);
    expect(model.supported).toBe(true);
    expect(model.title).not.toContain("SpanFinished");
    expect(model.panels[1].select?.({ output: "visible" })).toBe("visible");
  });
  it("keeps previews bounded and makes missing bodies explicit", () => {
    expect(processPreview({ content: "x".repeat(5000) })).toHaveLength(360);
    expect(processPreview(null)).toBe("未提供正文");
    expect(processPreview([{ role: "system", content: "policy".repeat(1000) }, { role: "user", content: "本轮请求" }])).toBe("本轮请求");
  });
  it("shows structured tool arguments without escaped JSON or provider wrappers", () => {
    const model = agentProcess(buildRunView([event(1, { data: { process_kind: "model_call" } })]).steps[0]);
    expect(model.panels[1].select?.({ visible_response: { tool_calls: [{ id: "call", type: "function", function: { name: "lookup", arguments: '{"query":"中文"}' } }] } })).toEqual({
      tool_calls: [{ tool_call_id: "call", name: "lookup", arguments: { query: "中文" } }],
    });
  });
  it("keeps orphan context/resource events readable and retains custom span outputs", () => {
    const context = agentProcess(buildRunView([event(1, { kind: "ContextSnapshotPrepared", data: { process_kind: "context" } })]).steps[0]);
    expect(context.supported).toBe(false);
    const resource = agentProcess(buildRunView([event(1, { kind: "InputResourcesDispatched", data: { process_kind: "resources" } })]).steps[0]);
    expect(resource.supported).toBe(false);
    const custom = agentProcess(buildRunView([event(1, { kind: "SpanFinished", phase: "finish", name: "检查资料", data: { process_kind: "research" } })]).steps[0]);
    expect(custom.panels[1].select?.({ findings: ["已取得资料"], capture_state: "available" })).toEqual({ findings: ["已取得资料"] });
  });
});
