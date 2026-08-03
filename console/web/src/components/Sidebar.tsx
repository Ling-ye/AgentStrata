import { Menu } from "@arco-design/web-react";
import {
  IconApps,
  IconExperiment,
  IconHome,
  IconRobot,
  IconSettings,
  IconTool,
} from "@arco-design/web-react/icon";

export type PageKey = "overview" | "services" | "bots" | "tools" | "evals" | "settings";

interface Props {
  current: PageKey;
  onChange: (key: PageKey) => void;
}

const NAV_ITEMS = [
  { itemKey: "overview" as const, text: "总览", icon: <IconHome /> },
  { itemKey: "services" as const, text: "服务管理", icon: <IconApps /> },
  { itemKey: "bots" as const, text: "机器人实例", icon: <IconRobot /> },
  { itemKey: "tools" as const, text: "组件目录", icon: <IconTool /> },
  { itemKey: "evals" as const, text: "评测中心", icon: <IconExperiment /> },
  { itemKey: "settings" as const, text: "设置", icon: <IconSettings /> },
];

export default function Sidebar({ current, onChange }: Props) {
  return (
    <Menu
      className="console-sidebar"
      selectedKeys={[current]}
      onClickMenuItem={(key) => onChange(key as PageKey)}
    >
      {NAV_ITEMS.map((item) => (
        <Menu.Item key={item.itemKey}>
          {item.icon}
          {item.text}
        </Menu.Item>
      ))}
    </Menu>
  );
}
