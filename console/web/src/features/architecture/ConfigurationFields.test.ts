import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import ConfigurationFields, { ConfigurationValues } from "./ConfigurationFields";
import type { InspectionEntity } from "./workbenchModel";

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
});
