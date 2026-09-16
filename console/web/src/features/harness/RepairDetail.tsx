import { DeliveryPanel } from "./DeliveryPanel";
import { deliveryActive } from "./api";
import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Empty, Space, Spin, Table, Tag, Typography } from "@arco-design/web-react";
import { ACTIVE, ATTEMPT_LABELS, harnessApi, sourceLabel, stageLabel } from "./api";
import type { RepairTask } from "./api";
import { HarnessTraces } from "../traces/TracePanel";
import { RepairProgress } from "./RepairProgress";
const { Text } = Typography;
const jsonStyle = { whiteSpace: "pre-wrap", overflowWrap: "anywhere", maxHeight: 420, overflow: "auto" } as const;

export function RepairDetail({ taskId, onRestart, onSelect }: { taskId: string; onRestart: (task: RepairTask) => void; onSelect?: (id: string) => void }) {
  const client = useQueryClient();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [showEvidence, setShowEvidence] = useState(false);
  const query = useQuery({ queryKey: ["harness-task", taskId], queryFn: ({ signal }) => harnessApi.get(taskId, signal),
    retry: false, refetchInterval: value => deliveryActive(value.state.data) ? 2000 : false });
  const evidence = useQuery({ queryKey: ["harness-evidence", taskId], queryFn: () => harnessApi.evidence(taskId), enabled: showEvidence, retry: false });
  const task = query.data;
  async function action(value: "cancel" | "resume" | "continue") {
    setBusy(true); setError("");
    try {
      const result = await harnessApi.action(taskId, value);
      client.setQueryData(["harness-task", result.task_id], result);
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
      await client.invalidateQueries({ queryKey: ["harness-history"] });
    } catch (value) { setError(value instanceof Error ? value.message : String(value)); }
    finally { setBusy(false); }
  }
  if (query.isPending) return <Spin tip="读取修复记录…" />;
  if (!task) return <Alert type="error" content={String(query.error)} />;
  const currentPipeline = task.pipeline_version === 6;
  return <Space direction="vertical" size={16} style={{ width: "100%", minWidth: 0 }}>
    {(error || query.isError) && <Alert type="error" content={error || String(query.error)} />}
    <Text copyable>{task.task_id}</Text><Text>{sourceLabel(task)}</Text>
    {task.source.case_instance_id && <Text copyable>Case 实例 ID：{task.source.case_instance_id}</Text>}
    <RepairProgress key={taskId} task={task} refreshTask={() => query.refetch()} />
    {task.continued_from && <Text copyable>接续自：{task.continued_from}（已累计原任务用时）</Text>}
    {!currentPipeline && !ACTIVE.includes(task.status) && <Alert type="info" content="此任务使用旧执行环境，仅保留历史记录；新流程请从来源重新创建任务。" />}
    {currentPipeline && task.next_action === "upload_image" && <section aria-label="补充原图">
      <Alert type="warning" content="请提供原任务中的图片。上传后自动继续，无需判断技术方案；原图仅保存在私有材料中。" />
      <input aria-label="选择原图并继续" type="file" accept="image/png,image/jpeg,image/gif,image/webp" disabled={busy}
        onChange={event => void upload(event.target.files?.[0])} />
    </section>}
    {task.acceptance && <section aria-label="完整验收覆盖"><Text bold>完整验收覆盖</Text>
      {task.acceptance.items.map(item => <p key={item.id}><Tag color={task.acceptance_coverage?.[item.id]?.passed ? "green" : "orange"}>
        {task.acceptance_coverage?.[item.id]?.passed ? "通过" : "待验证"}</Tag>{item.text || "原任务预期"}</p>)}
    </section>}
    {!!task.preparation_revisions?.length && <section aria-label="准备修订记录"><Text bold>准备修订记录</Text>
      {task.preparation_revisions.map(revision => <p key={revision.revision}>第 {revision.revision} 版 · {revision.status === "validated" ? "试运行通过" : revision.status === "running" ? "自动修正中" : "已记录失败"}
        {revision.error && `：${revision.error.code} · ${revision.error.message}`}</p>)}
    </section>}
    <DeliveryPanel task={task} refresh={() => query.refetch()} />
    {task.message && <Alert type={task.status === "fixed" ? "success" : "info"} content={task.message} />}
    {!!task.source.warnings?.length && <Alert type="warning" title="来源证据缺口"
      content={task.source.warnings.map(warning => warning.message).join("；")} />}
    {task.verification_plan && <Text type="secondary">验证范围：{task.verification_plan.real_agent ? "真实 Agent 与隔离工具环境" : "冻结的确定性复现测试"}；每组 {task.verification_plan.repetitions} 次，{task.verification_plan.checks.length} 个检查项{task.planned_agent_trials ? `，最多 ${task.planned_agent_trials} 次 Agent 执行` : ""}。</Text>}
    {task.hypothesis && <section><Text bold>根因假设与验收预期</Text><p>{task.hypothesis.reason}</p><p>{task.hypothesis.expected_behavior}</p></section>}
    {task.source.feedback && <section aria-label="本次修复补充内容">
      <Text bold>本次修复补充内容</Text>
      {task.source.feedback.repair_hint && <div style={{ marginTop: 12 }}><Text bold>修复提示</Text>
        <div style={jsonStyle}>{task.source.feedback.repair_hint}</div></div>}
      {task.source.feedback.expected_behavior && <div style={{ marginTop: 12 }}><Text bold>参考答案／预期行为</Text>
        <div style={jsonStyle}>{task.source.feedback.expected_behavior}</div></div>}
      <Text type="secondary">这是发起时提供的验收期望与调查线索；需要更正时，请重新发起修复。</Text>
    </section>}
    {task.status === "fixed" && task.candidate_available === false && <Alert type="warning" content="验证后的工作区已变化或不可用，不能直接复用旧结论。" />}
    <div style={{ overflowWrap: "anywhere" }}><Text>基线：{task.base_commit}</Text>
      {task.branch && <p><Text copyable>分支：{task.branch}</Text></p>}
      {task.worktree && <p><Text copyable>工作区：{task.worktree}</Text></p>}
      {task.verified_at && <Text>验证时间：{new Date(task.verified_at * 1000).toLocaleString()}</Text>}</div>
    <Space wrap>{ACTIVE.includes(task.status) && <Button status="danger" loading={busy} onClick={() => void action("cancel")}>取消</Button>}
      {["blocked", "interrupted", "cancelled"].includes(task.status) && currentPipeline && !task.source.blockers?.length &&
        <Button loading={busy} onClick={() => void action("resume")}>检查并继续</Button>}
      {!ACTIVE.includes(task.status) && task.source.kind === "robot_task" && (currentPipeline && task.status !== "waiting_input") &&
        <Button loading={busy} onClick={() => void action("continue")}>接续修复（累计预算）</Button>}
      {!ACTIVE.includes(task.status) && (currentPipeline && task.status !== "waiting_input") && (task.source.run_id || task.source.case_instance_id) &&
        <Button disabled={busy} onClick={() => onRestart(task)}>重新发起修复</Button>}
      <Button onClick={() => void query.refetch()}>刷新状态</Button>
      {task.source.test_sha256 && <a href={`/api/harness/tasks/${encodeURIComponent(taskId)}/reproducer`} download>下载冻结复现测试</a>}</Space>
    {task.source.diagnosis && <Alert type="info" title="复现依据" content={`${task.source.diagnosis.reason}；预期行为：${task.source.diagnosis.expected_behavior}`} />}
    {(task.delivery || task.review_and_commit) && <section><Text bold>AI 审核</Text>
      {(task.attempts ?? []).filter(attempt => attempt.review).map(attempt => <div key={attempt.number} style={{ marginTop: 12 }}>
        <Alert type={attempt.review?.decision === "approved" ? "success" : "warning"}
          title={attempt.review?.decision === "approved" ? "审核通过" : attempt.review?.decision === "rejected" ? "AI 审核认为问题未解决" : "AI 审核未能确认修复"}
          content={<><div>问题：{attempt.review?.problem || "未指出未修复问题"}</div><div>理由：{attempt.review?.reason || "等待审核结果"}</div>
            <div>证据：{attempt.review?.evidence_refs?.join("、") || "等待证据引用"}</div></>} />
      </div>)}
      {!task.attempts?.some(attempt => attempt.review) && <p><Text type="secondary">目标和保护集验证通过后开始一次只读审核。</Text></p>}
    </section>}
    {task.local_commit && <section style={{ overflowWrap: "anywhere" }}><Text bold>本地提交与回归收录</Text>
      <p><Text copyable>{task.local_commit.sha}</Text></p>
      <p><Text copyable>{task.regression?.path || task.regression?.case_ref || task.regression?.id}</Text></p>
      <Text>{task.commit_in_main === true ? "该提交已包含在本地 main 中" : task.commit_in_main === false ? "该提交尚未包含在本地 main 中" : "当前无法确认本地 main 是否包含该提交"}；远端状态未查询。</Text>
    </section>}
    {task.commit_state === "unconfirmed" && <Alert type="warning" content="Git 分支已产生提交，但回执尚未完成核验；继续时只核对并补记，不能重复提交。" />}
    <HarnessTraces key={taskId} taskId={taskId} active={ACTIVE.includes(task.status)} />
    <Text bold>自检与复测记录</Text>
    {Object.keys(task.evaluations ?? {}).length ? <Table size="small" rowKey="phase" pagination={false} scroll={{ x: 620 }}
      data={Object.entries(task.evaluations ?? {}).map(([phase, value]) => ({ phase, ...value }))} columns={[
        { title: "阶段", render: (_, row) => stageLabel(row.phase) },
        { title: "验证记录", render: (_, row) => <Text copyable>{row.evaluation_id}</Text> },
        { title: "进度", render: (_, row) => row.complete ? `通过 ${row.passed_cases?.length ?? 0} / ${(row.case_ids?.length ?? 0)}` : "执行中" },
      ]} /> : <Empty description="完成来源自检后，将在这里显示复现和回归验证记录" />}
    {!!task.commit_checks?.length && <section><Text bold>宿主检查记录</Text>{task.commit_checks.map((check, index) => <details key={index}><summary>{check.label} · {check.exit_code === 0 ? "通过" : "未通过"}</summary><pre style={jsonStyle}>{check.output}</pre></details>)}</section>}
    <Text bold>修复尝试</Text>
    <Table rowKey="number" pagination={false} scroll={{ x: 600 }} data={task.attempts ?? []} columns={[
      { title: "次数", dataIndex: "number", width: 60 }, { title: "状态", width: 110, render: (_, row) => ATTEMPT_LABELS[row.status] ?? row.status },
      { title: "验收", render: (_, row) => row.verification ? `通过 ${row.verification.passed_cases?.length ?? 0} 项，未通过 ${row.verification.failed_cases?.length ?? 0} 项，退化 ${row.regressions?.length ?? 0} 项` : row.error || "等待结果" },
      { title: "补丁", width: 65, render: (_, row) => row.patch_sha256 ? <a href={`/api/harness/tasks/${encodeURIComponent(taskId)}/attempts/${row.number}/patch`} download>下载</a> : "—" },
    ]} />
    <details onToggle={event => setShowEvidence(event.currentTarget.open)}><summary>来源证据快照</summary>
      {evidence.isError ? <Alert type="error" content={String(evidence.error)} /> : <pre style={jsonStyle}>{evidence.data ? JSON.stringify(evidence.data, null, 2) : "正在加载…"}</pre>}</details>
    <details><summary>完整修复记录（含公开执行输出）</summary><pre style={jsonStyle}>{JSON.stringify(task, null, 2)}</pre></details>
  </Space>;
}
