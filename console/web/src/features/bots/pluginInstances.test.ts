import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { QueryClient, QueryClientProvider, type QueryObserverOptions } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import ReadOnlyBotConfiguration from "../../components/ReadOnlyBotConfiguration";
import BotsPage from "../../pages/BotsPage";
import { api } from "../../api";
import type { BotInstance } from "../../types";
import type { Inspection } from "../architecture/workbenchModel";
import { useBotsOverview } from "./useBotsOverview";

const plugin: BotInstance = { instance_id: "plugin:fixture:main", display_name: "Avatar fixture", platform: "",
  origin: { kind: "plugin", plugin_id: "fixture", version: "1.0.0", agent_id: "main" },
  runtime_kind: "gateway", unit: "", is_deployed: false, wsl_home: "", workspace_root: "", log_dir: "",
  env_file: "/private/plugin/config/runtime.env", cc_home: "", project_name: "" };

function client() {
  return new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity, staleTime: Infinity } } });
}

beforeEach(() => {
  vi.stubGlobal("window", { innerWidth: 1200, location: { hash: "#bots?instance=plugin%3Afixture%3Amain&tab=configuration&layer=agent" },
    sessionStorage: { getItem: () => null }, addEventListener: vi.fn(), removeEventListener: vi.fn() });
});
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

it("renders current plugin configuration without constructing an editor or offering writes", () => {
  const queryClient = client();
  const config: Inspection = { current: { layers: [], configuration_revision: "fixture-revision", entities: [
    { id: "mcp:avatar", name: "Avatar MCP", layer: "capability", config: { url: "http://127.0.0.1:19600/mcp" },
      configured: true, loaded: null, connected: null, available: true },
  ] }, loaded: null, loaded_meta: null, execution: null, errors: [], generated_at: 1, pending_changes: false, loaded_stale: true };
  queryClient.setQueryData(["inspection", plugin.instance_id], config);
  const html = renderToStaticMarkup(createElement(QueryClientProvider, { client: queryClient },
    createElement(ReadOnlyBotConfiguration, { instanceId: plugin.instance_id, visible: true, infoVisible: false, onInfoClose: () => {} })));
  expect(html).toContain("Avatar MCP");
  expect(html).toContain("只读展示配置");
  const buttons = [...html.matchAll(/<button\b[^>]*>([\s\S]*?)<\/button>/g)].map((match) => match[1].replace(/<[^>]*>/g, ""));
  for (const label of ["添加 MCP", "保存配置", "保存并重启", "启用 Avatar"]) expect(buttons).not.toContain(label);
  expect(html).not.toContain("未保存的配置");
  expect(queryClient.getQueryData(["bot-tools", plugin.instance_id])).toBeUndefined();
  expect(queryClient.getQueryCache().getAll().map((query) => query.queryKey)).toEqual([["inspection", plugin.instance_id]]);
  queryClient.clear();
});

it("shows plugin source and task/configuration tabs without deployment, service or binding controls", () => {
  const queryClient = client();
  queryClient.setQueryData(["bots"], [plugin]);
  const html = renderToStaticMarkup(createElement(QueryClientProvider, { client: queryClient }, createElement(BotsPage, { loadError: null })));
  expect(html).toContain("由插件管理");
  expect(html).toContain("fixture · 1.0.0");
  expect(html).toContain("只读分层配置");
  for (const label of ["首次部署", "注册服务", "更新并重启", "服务日志", "运行状态"]) expect(html).not.toContain(label);
  const statuses = queryClient.getQueryCache().find({ queryKey: ["bot-status", plugin.instance_id] });
  expect((statuses?.options as QueryObserverOptions).enabled).toBe(false);
  queryClient.clear();
});

it("never polls or manually refreshes plugin service status while preserving managed instance refresh", async () => {
  const queryClient = client();
  const managed: BotInstance = { ...plugin, instance_id: "managed", origin: { kind: "repository" }, is_deployed: true };
  queryClient.setQueryData(["bots"], [plugin, managed]);
  const status = vi.spyOn(api, "status").mockResolvedValue({} as Awaited<ReturnType<typeof api.status>>);
  let model: ReturnType<typeof useBotsOverview>;
  function Probe() { model = useBotsOverview(); return null; }
  renderToStaticMarkup(createElement(QueryClientProvider, { client: queryClient }, createElement(Probe)));
  for (const key of ["bot-status", "bot-inventory"]) {
    expect((queryClient.getQueryCache().find({ queryKey: [key, plugin.instance_id] })?.options as QueryObserverOptions).enabled).toBe(false);
    expect((queryClient.getQueryCache().find({ queryKey: [key, managed.instance_id] })?.options as QueryObserverOptions).enabled).toBe(true);
  }
  await model!.refreshStatuses();
  expect(status.mock.calls).toEqual([[managed.instance_id]]);
  queryClient.clear();
});
