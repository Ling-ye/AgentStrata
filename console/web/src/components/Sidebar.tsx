import { StudioShell, type StudioGroup } from "../features/applications/StudioSidebar";
import type { ReactNode } from "react";

export type PageKey =
  | "applications"
  | "create"
  | "overview"
  | "services"
  | "bots"
  | "tools"
  | "evals"
  | "harness"
  | "code-health"
  | "schedules"
  | "models"
  | "settings";
const platform: StudioGroup[] = [
  {
    title: "模型与账号",
    items: [
      { label: "模型连接与凭据", href: "#models" },
      { label: "共享模型方案与后台用途", href: "#models?section=profiles" },
      { label: "开发账号", href: "#models?section=account" },
    ],
  },
  {
    title: "共享资源",
    items: [
      { label: "共享服务与诊断", href: "#services" },
      { label: "组件目录", href: "#tools" },
    ],
  },
  { title: "项目治理", items: [{ label: "项目代码熵回收", href: "#code-health" }] },
  { title: "工作台", items: [{ label: "工作台设置", href: "#settings" }] },
];
export default function PlatformSidebar({ current, children }: { current: string; children: ReactNode }) {
  return (
    <StudioShell groups={platform} current={current}>
      {children}
    </StudioShell>
  );
}
