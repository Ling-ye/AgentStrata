export const APPLICATION_TABS = ["overview", "use", "configuration", "records", "improve"] as const;
export type ApplicationTab = typeof APPLICATION_TABS[number];
export function applicationLocation(hash: string) {
  const [page, query] = hash.replace(/^#/, "").split("?");
  const params = new URLSearchParams(query);
  const tab = params.get("tab") ?? "overview";
  return { id: page === "applications" ? params.get("application") ?? "" : "", tab: APPLICATION_TABS.includes(tab as ApplicationTab) ? tab as ApplicationTab : "overview" as ApplicationTab };
}
export function applicationHref(id: string, tab: ApplicationTab = "overview") {
  return "#applications?" + new URLSearchParams({ application: id, tab });
}
export function leavesApplication(hash: string, id: string) { return applicationLocation(hash).id !== id; }
export function localLink(value?: string) { return value?.startsWith("#") ? value : undefined; }
export function typeLabel(type: string) { return ({ qq_official: "QQ 官方机器人", live2d: "桌面伙伴 · Live2D", companion: "桌面伙伴 · Live2D", desktop: "桌面应用" } as Record<string, string>)[type] ?? type; }
export function stateLabel(value: string) { return ({ active: "进程运行中", running: "运行中", running_unverified: "进程运行，待实聊验证", stopped: "已停止", inactive: "已停止", not_created: "待配置", configured: "已配置", starting: "正在启动", stopping: "正在停止", failed: "失败", connected: "已连接", disconnected: "未连接", unknown: "未知", unverified: "尚未验证", unavailable: "不可用", unobserved: "尚未读取", not_checked: "尚未检查", activating: "正在启动", deactivating: "正在停止", runtime_unavailable: "运行状态不可用", console_thinking: "正在处理" } as Record<string, string>)[value] ?? value; }

export function managementStateLabel(state: string): string {
  return ({ "console-thinking": "正在整理需求", configure: "配置提案待确认", applied: "提案已应用", discuss: "建议与说明", failed: "处理失败", queued: "排队中", running: "处理中", completed: "已完成", canceled: "已取消" } as Record<string, string>)[state] || "历史状态：" + state;
}