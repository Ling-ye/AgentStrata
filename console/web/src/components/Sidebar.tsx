import { Menu } from "@arco-design/web-react";
import { IconApps, IconPlus, IconSettings, IconTool } from "@arco-design/web-react/icon";
export type PageKey = "applications" | "create" | "overview" | "services" | "bots" | "tools" | "evals" | "harness" | "code-health" | "schedules" | "models" | "settings";
interface Props { current: PageKey; onChange: (key: PageKey) => void }
const advanced = [
  ["overview", "运行总览"], ["services", "服务管理"], ["bots", "实例配置与轨迹"], ["schedules", "定时任务"],
  ["tools", "组件目录"], ["evals", "测评中心"], ["harness", "AI Harness 改进"], ["code-health", "代码熵回收"], ["settings", "设置"],
] as const;
export default function Sidebar({ current, onChange }: Props) {
  return <Menu className="console-sidebar" selectedKeys={[current]}
    defaultOpenKeys={advanced.some(([key]) => key === current) ? ["advanced"] : []}
    onClickMenuItem={key => onChange(key as PageKey)}>
    <Menu.Item key="applications"><IconApps />我的应用</Menu.Item>
    <Menu.Item key="create"><IconPlus />创建应用</Menu.Item>
    <Menu.Item key="models"><IconSettings />模型与连接</Menu.Item>
    <Menu.SubMenu key="advanced" title={<><IconTool />高级管理</>}>
      {advanced.map(([key, title]) => <Menu.Item key={key}>{title}</Menu.Item>)}
    </Menu.SubMenu>
  </Menu>;
}
