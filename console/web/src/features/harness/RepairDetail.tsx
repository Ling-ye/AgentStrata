import { DeliveryPanel } from "./DeliveryPanel";
import { deliveryActive } from "./api";
import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Space, Spin, Typography } from "@arco-design/web-react";
import { ACTIVE, harnessApi, sourceLabel } from "./api";
import type { RepairTask } from "./api";
import { RepairFlow } from "./RepairFlow";
import { CommandLogs } from "./CommandLogs";
import { TaskDetailState } from "../architecture/observationDetailState";
import { RepairProgress } from "./RepairProgress";
import { GovernanceReport } from "./GovernanceReport";
const { Text } = Typography;
const jsonStyle = { whiteSpace: "pre-wrap", overflowWrap: "anywhere", maxHeight: 420, overflow: "auto" } as const;

export function RepairDetail({ taskId, onRestart, onSelect }: { taskId: string; onRestart: (task: RepairTask) => void; onSelect?: (id: string) => void }) {
  const client = useQueryClient();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [showEvidence, setShowEvidence] = useState(false);
  const [showRaw, setShowRaw] = useState(false);
  const [commandSelection, setCommandSelection] = useState({ open: false, source: "" });
  const query = useQuery({ queryKey: ["harness-task-summary", taskId], queryFn: ({ signal }) => harnessApi.summary(taskId, signal),
    retry: false, refetchInterval: value => deliveryActive(value.state.data) ? 2000 : false });
  const evidence = useQuery({ queryKey: ["harness-evidence", taskId], queryFn: ({ signal }) => harnessApi.evidence(taskId, signal), enabled: showEvidence, retry: false });
  const raw = useQuery({ queryKey: ["harness-task", taskId], queryFn: ({ signal }) => harnessApi.get(taskId, signal), enabled: showRaw, retry: false });
  const task = query.data;
  async function action(value: "cancel" | "resume" | "continue") {
    setBusy(true); setError("");
    try {
      const result = await harnessApi.action(taskId, value);
      client.setQueryData(["harness-task", result.task_id], result);
      await client.invalidateQueries({ queryKey: ["harness-task-summary", taskId] });
      if (result.task_id !== taskId) onSelect?.(result.task_id);
      await client.invalidateQueries({ queryKey: ["harness-history"] });
    } catch (value) { setError(value instanceof Error ? value.message : String(value)); }
    finally { setBusy(false); }
  }
  async function upload(file?: File) {
    if (!file) return;
    setBusy(true); setError("");
    try {
      const result = await harnessApi.image(taskId, file);
      client.setQueryData(["harness-task", taskId], result);
      await client.invalidateQueries({ queryKey: ["harness-task-summary", taskId] });
      await client.invalidateQueries({ queryKey: ["harness-history"] });
    } catch (value) { setError(value instanceof Error ? value.message : String(value)); }
    finally { setBusy(false); }
  }
  if (query.isPending) return <Spin tip="读取修复记录…" />;
  if (!task) return <Alert type="error" content={String(query.error)} />;
  const currentPipeline = task.archived === false;
  return <TaskDetailState instanceId="harness" runId={taskId}><Space direction="vertical" size={16} style={{ width: "100%", minWidth: 0 }}>
    {(error || query.isError) && <Alert type="error" content={error || String(query.error)} />}
    <Text copyable>{task.task_id}</Text><Text>{sourceLabel(task)}</Text>
    {task.source.case_instance_id && <Text copyable>Case 实例 ID：{task.source.case_instance_id}</Text>}
    <RepairProgress key={taskId} task={task} refreshTask={() => query.refetch()} title="任务概览" summaryOnly />
    {task.continued_from && <Text copyable>接续自：{task.continued_from}（已累计原任务用时）</Text>}
    {!currentPipeline && !ACTIVE.includes(task.status) && <Alert type="info" content="此任务使用旧执行环境，仅保留历史记录；新流程请从来源重新创建任务。" />}
    {currentPipeline && task.next_action === "upload_image" && <section aria-label="补充原图">
      <Alert type="warning" content="请提供原任务中的图片。上传后自动继续，无需判断技术方案；原图仅保存在私有材料中。" />
      <input aria-label="选择原图并继续" type="file" accept="image/png,image/jpeg,image/gif,image/webp" disabled={busy}
        onChange={event => void upload(event.target.files?.[0])} />
    </section>}
    {task.message && <Alert type={task.status === "fixed" ? "success" : "info"} content={task.message} />}
    {!!task.source.warnings?.length && <Alert type="warning" title="来源证据缺口"
      content={task.source.warnings.map(warning => warning.message).join("；")} />}
    {task.status === "fixed" && task.candidate_available === false && <Alert type="warning" content="验证后的工作区已变化或不可用，不能直接复用旧结论。" />}
    <Space wrap>{currentPipeline && ACTIVE.includes(task.status) && <Button status="danger" loading={busy} onClick={() => void action("cancel")}>取消</Button>}
      {["blocked", "interrupted", "cancelled"].includes(task.status) && currentPipeline && !task.source.blockers?.length &&
        <Button loading={busy} onClick={() => void action("resume")}>检查并继续</Button>}
      {!ACTIVE.includes(task.status) && task.source.kind === "robot_task" && (currentPipeline && task.status !== "waiting_input") &&
        <Button loading={busy} onClick={() => void action("continue")}>接续修复（累计预算）</Button>}
      {!ACTIVE.includes(task.status) && task.status !== "waiting_input" && (task.source.run_id || task.source.case_instance_id) &&
        <Button disabled={busy} onClick={() => onRestart(task)}>重新发起修复</Button>}
      {!ACTIVE.includes(task.status) && task.source.kind === "code_health" &&
        <Button disabled={busy} onClick={() => onRestart(task)}>重新发起熵回收</Button>}
      {!!task.candidate_checkpoint?.changed_files.length && <a
        href={`/api/harness/tasks/${encodeURIComponent(taskId)}/attempts/${task.candidate_checkpoint.number}/patch`} download>下载保留候选</a>}
      <Button onClick={() => void query.refetch()}>刷新状态</Button>
      {task.source.test_sha256 && <a href={`/api/harness/tasks/${encodeURIComponent(taskId)}/reproducer`} download>下载冻结复现测试</a>}</Space>
    {task.commit_state === "unconfirmed" && <Alert type="warning" content="提交回执尚未核验；继续时会检查实际提交状态。" />}
    {task.source.kind === "code_health" && <GovernanceReport taskId={taskId} active={deliveryActive(task)} />}
    <RepairFlow key={taskId} taskId={taskId} active={deliveryActive(task)} commands={source => {
      setCommandSelection({ open: true, source });
      window.requestAnimationFrame(() => document.getElementById("repair-command-logs")?.scrollIntoView({ behavior: "smooth", block: "start" }));
    }} />
    <DeliveryPanel task={task} refresh={() => query.refetch()} actionsOnly />
    <CommandLogs key={`commands:${taskId}`} taskId={taskId} active={deliveryActive(task)} selection={commandSelection} setSelection={setCommandSelection} />
    <Text bold>原始资料</Text>
    <details><summary>任务与源码信息</summary>
    <div style={{ overflowWrap: "anywhere" }}><Text>基线：{task.base_commit}</Text>
      {task.branch && <p><Text copyable>分支：{task.branch}</Text></p>}
      {task.worktree && <p><Text copyable>工作区：{task.worktree}</Text></p>}
      {task.verified_at && <Text>验证时间：{new Date(task.verified_at * 1000).toLocaleString()}</Text>}</div>
    </details>
    <details onToggle={event => setShowEvidence(event.currentTarget.open)}><summary>来源证据快照</summary>
      {evidence.isError ? <Alert type="error" content={String(evidence.error)} /> : <pre style={jsonStyle}>{evidence.data ? JSON.stringify(evidence.data, null, 2) : "正在加载…"}</pre>}</details>
    <details onToggle={event => setShowRaw(event.currentTarget.open)}><summary>完整修复记录（含公开执行输出）</summary>
      {raw.isError ? <Alert type="error" content={String(raw.error)} /> : <pre style={jsonStyle}>{raw.data ? JSON.stringify(raw.data, null, 2) : "正在加载…"}</pre>}</details>
  </Space></TaskDetailState>;
}
