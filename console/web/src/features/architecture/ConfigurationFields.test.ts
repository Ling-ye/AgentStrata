import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import ConfigurationFields, { ConfigurationValues } from "./ConfigurationFields";
import type { InspectionEntity } from "./workbenchModel";
import { EntityState } from "./ConfigurationPane";

describe("read-only configuration details", () => {
  it("uses readable fields and resolved environment values without editable inputs", () => {
    const entity: InspectionEntity = { id: "channel:qq", layer: "channel", name: "QQ", configured: true, loaded: null, connected: null, available: null,
      config: { account_env: "QQ_ACCOUNT", enabled: false, timeout_seconds: 30 }, environment: { QQ_ACCOUNT: "123456" } };
    const html = renderToStaticMarkup(createElement(ConfigurationFields, { entity }));
    expect(html).toContain("机器人账号");
    expect(html).toContain("123456");
    expect(html).toContain("超时（秒）");
    expect(html).toContain(">否<");
    expect(html).toContain("配置来源");
    expect(html).not.toContain("<input");
    expect(html).not.toContain("structured-toolbar");
  });
  it("distinguishes missing values, empty strings, false, zero and nested collections", () => {
    const html = renderToStaticMarkup(createElement(ConfigurationValues, { value: { absent: null, empty: "", flag: false, zero: 0, list: [], nested: [{ model: "fixture" }] } }));
    for (const label of ["未配置", "空字符串", ">否<", ">0<", "空列表", "fixture"]) expect(html).toContain(label);
  });
  it("escapes operator text instead of treating config strings as markup", () => {
    const html = renderToStaticMarkup(createElement(ConfigurationValues, { value: { command: "<script>fixture</script>" } }));
    expect(html).toContain("&lt;script&gt;");
    expect(html).not.toContain("<script>");
  });
  it("shows resolved values with sources, meaningful empty values and only differing live fields", () => {
    const entity: InspectionEntity = { id: "model-slot:code", layer: "agent", name: "Code", configured: true, loaded: true, connected: null, available: null,
      config: { model: "saved-model", reasoning_effort: "medium", enabled: false, hard_iteration_cap: null },
      field_sources: { model: "环境覆盖 FIXTURE_CODE_MODEL" }, runtime: { code_model: "live-model", code_reasoning_effort: "medium" } };
    const html = renderToStaticMarkup(createElement(ConfigurationFields, { entity, inline: true }));
    for (const value of ["saved-model", "live-model", "服务当前值", "环境覆盖 FIXTURE_CODE_MODEL", "允许模型切换命令", "未设置限制"]) expect(html).toContain(value);
    expect(html.match(/服务当前值/g)).toHaveLength(1);
    expect(html).not.toContain("配置来源");
    const stale = renderToStaticMarkup(createElement(ConfigurationFields, { entity: { ...entity, runtime_stale: true } }));
    expect(stale).not.toContain("live-model");
  });
  it("does not invent load states for static configuration or stale connections", () => {
    const entity: InspectionEntity = { id: "model-slot:chat", layer: "agent", name: "Chat", configured: true, loaded: true, connected: null, available: null, config: {} };
    expect(renderToStaticMarkup(createElement(EntityState, { entity }))).not.toContain("已加载");
    expect(renderToStaticMarkup(createElement(EntityState, { entity: { ...entity, loaded: null } }))).not.toContain("未知");
    const mcp = { ...entity, id: "mcp:search", connected: false };
    expect(renderToStaticMarkup(createElement(EntityState, { entity: mcp }))).toContain("未连接");
    const stale = renderToStaticMarkup(createElement(EntityState, { entity: { ...mcp, runtime_stale: true } }));
    expect(stale).not.toContain("未连接");
    expect(stale).not.toContain("已加载");
  });
});
