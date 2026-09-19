import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Select, Space, Spin, Tag, Typography } from "@arco-design/web-react";
import { governanceApi, RUN_LABELS, runActive, stopLabel, type GovernanceRun } from "./governance";
import { deliveryLabel, REPAIR_LABELS } from "./api";
import { RepairDetail } from "./RepairDetail";

export function GovernanceRunDetail({ runId, onRestart }: { runId: string; onRestart: (run: GovernanceRun) => void }) {
  const client = useQueryClient();
  const [selected, setSelected] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const query = useQuery({ queryKey: ["governance-run", runId], queryFn: ({ signal }) => governanceApi.run(runId, signal), retry: false, refetchInterval: 5000 });
  const run = query.data;
  async function action(value: "cancel" | "resume") {
    setBusy(true); setError("");
    try {
      const result = await governanceApi.action(runId, value);
      client.setQueryData(["governance-run", runId], result);
      await client.invalidateQueries({ queryKey: ["governance-history"] });
      await client.invalidateQueries({ queryKey: ["harness-task-summary"] });
    } catch (value) { setError(value instanceof Error ? value.message : String(value)); }
    finally { setBusy(false); }
  }
  if (query.isPending) return <Spin tip="读取回收批次…" />;
  if (!run) return <Alert type="error" content={String(query.error)} />;
  const taskId = selected || run.current_task_id;
  return <Space direction="vertical" size={16} style={{ width: "100%", minWidth: 0 }}>
    <Typography.Text copyable>{run.run_id}</Typography.Text>
    <Space wrap><Tag>{RUN_LABELS[run.status] ?? run.status}</Tag><Typography.Text>{stopLabel(run.options.stop_condition)}</Typography.Text>
      <Typography.Text>已发现 {run.found_count} 项 · 已合并 {run.merged_count} 项</Typography.Text>
      <Typography.Text>累计执行 {Math.round(run.elapsed_seconds)} 秒（不含等待）</Typography.Text></Space>
    {run.message && <Alert type={run.status === "blocked" ? "warning" : "info"} content={run.message} />}
    {(error || query.isError) && <Alert type="error" content={error || String(query.error)} />}
    <Space wrap>
      {(runActive(run) || run.status === "blocked") && <Button status="danger" loading={busy} disabled={run.status === "cancel_requested"}
        onClick={() => void action("cancel")}>取消整次回收</Button>}
      {["blocked", "cancelled"].includes(run.status) && <Button loading={busy} onClick={() => void action("resume")}>检查并恢复批次</Button>}
      {!runActive(run) && <Button disabled={busy} onClick={() => onRestart(run)}>使用相同设置重新发起</Button>}
    </Space>
    {!!run.tasks.length && <><Select aria-label="查看回收问题" value={taskId ?? undefined} onChange={setSelected}
      options={run.tasks.map(task => ({ value: task.task_id, label:
        `第 ${task.governance_sequence} 项 · ${task.governance_summary?.topic ?? "调查中"} · ${task.delivery ? deliveryLabel(task.delivery.state) : REPAIR_LABELS[task.status] ?? task.status}` }))} />
      <Space wrap>{run.tasks.filter(task => task.delivery?.pr_url).map(task => <a key={task.task_id} href={task.delivery!.pr_url} target="_blank" rel="noreferrer">
        第 {task.governance_sequence} 项 PR #{task.delivery!.pr_number} · {deliveryLabel(task.delivery!.state)}
      </a>)}</Space></>}
    {taskId && <RepairDetail key={taskId} taskId={taskId} managedRun onRestart={() => onRestart(run)} />}
  </Space>;
}
