import { useState } from "react";
import { Alert, Button, Descriptions, Space, Tag, Typography } from "@arco-design/web-react";
import { deliveryActive, deliveryLabel, harnessApi, type RepairTask } from "./api";

export function DeliveryPanel({ task, refresh, actionsOnly = false }: {
  task: Pick<RepairTask, "task_id" | "status" | "delivery" | "cleanup" | "archive">;
  refresh: () => unknown; actionsOnly?: boolean;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  if (!task.delivery) return null;
  const delivery = task.delivery;
  async function action(name: "retry-delivery" | "retry-cleanup" | "cancel") {
    setBusy(true); setError("");
    try { await harnessApi.action(task.task_id, name); await refresh(); }
    catch (value) { setError(String(value)); } finally { setBusy(false); }
  }
  const labels: Record<string, string> = { pending: "等待任务进程退出后清理", not_created: "未创建本地工作区", archived: "已归档", cleaned: "已清理", restored: "已恢复用于复验", blocked: "清理受阻" };
  return <section aria-label="PR 交付与清理" style={{ overflowWrap: "anywhere" }}>
    <Space wrap><Typography.Text bold>PR 交付</Typography.Text><Tag color={delivery.state === "merged" ? "green" : "blue"}>{deliveryLabel(delivery.state)}</Tag></Space>
    {!actionsOnly && <><Descriptions column={1} size="small" data={[
      { label: "目标", value: `${delivery.repository} → ${delivery.base_branch}` },
      { label: "PR", value: delivery.pr_url ? <a href={delivery.pr_url} target="_blank" rel="noreferrer">PR #{delivery.pr_number}</a> : "尚未创建" },
      { label: "验收提交", value: delivery.commit_sha ?? "等待生成" },
      { label: "合并提交", value: delivery.merge_sha ?? "尚未合并" },
      { label: "本地资源", value: labels[task.cleanup?.local ?? ""] ?? "待归档清理" },
      { label: "远端分支", value: task.cleanup?.remote === "cleaned" ? "已清理" : "PR 合并或关闭后清理" },
      { label: "证据归档", value: task.archive ? "已保存，可恢复；日志与补丁继续保留" : "等待归档" },
    ]} />
    {delivery.message && <Alert type={delivery.state === "merged" ? "success" : "info"} content={delivery.message} />}
    {!!delivery.checks?.length && <Space wrap>{delivery.checks.map(check => <Tag key={check.name} color={check.conclusion === "success" ? "green" : "orange"}>
      {check.url ? <a href={check.url} target="_blank" rel="noreferrer">{check.name}</a> : check.name} · {check.conclusion ?? check.status}
    </Tag>)}</Space>}
    {task.cleanup?.error && <Alert type="warning" content={task.cleanup.error} />}
    </>}<Space wrap style={{ marginTop: 12 }}>
      {["blocked", "retryable", "paused"].includes(delivery.state) && <Button loading={busy} onClick={() => void action("retry-delivery")}>重试交付</Button>}
      {task.cleanup?.local === "blocked" && <Button loading={busy} onClick={() => void action("retry-cleanup")}>重试清理</Button>}
      {deliveryActive(task) && !["queued", "running", "cancel_requested"].includes(task.status) && <Button status="danger" loading={busy} onClick={() => void action("cancel")}>停止自动交付</Button>}
    </Space>
    {error && <Alert type="error" content={error} />}
    <p><Typography.Text type="secondary">合并状态来自 GitHub；本地 main 和运行实例不会自动更新。</Typography.Text></p>
  </section>;
}
