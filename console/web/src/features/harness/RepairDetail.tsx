import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Empty, Space, Spin, Table, Tag, Typography } from "@arco-design/web-react";
import { ACTIVE, ATTEMPT_LABELS, harnessApi, repairStatusLabel, sourceLabel, stageLabel } from "./api";
const { Text } = Typography;
const jsonStyle = { whiteSpace: "pre-wrap", overflowWrap: "anywhere", maxHeight: 420, overflow: "auto" } as const;

export function RepairDetail({ taskId }: { taskId: string }) {
  const client = useQueryClient();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [showEvidence, setShowEvidence] = useState(false);
  const query = useQuery({ queryKey: ["harness-task", taskId], queryFn: ({ signal }) => harnessApi.get(taskId, signal),
    retry: false, refetchInterval: value => ACTIVE.includes(value.state.data?.status ?? "") ? 2000 : false });
  const evidence = useQuery({ queryKey: ["harness-evidence", taskId], queryFn: () => harnessApi.evidence(taskId), enabled: showEvidence, retry: false });
  const task = query.data;
  async function action(value: "cancel" | "resume") {
    setBusy(true); setError("");
    try {
      const result = await harnessApi.action(taskId, value);
      client.setQueryData(["harness-task", taskId], result);
      await client.invalidateQueries({ queryKey: ["harness-history"] });
    } catch (value) { setError(value instanceof Error ? value.message : String(value)); }
    finally { setBusy(false); }
  }
  if (query.isPending) return <Spin tip="读取修复记录…" />;
  if (!task) return <Alert type="error" content={String(query.error)} />;
  return <Space direction="vertical" size={16} style={{ width: "100%", minWidth: 0 }}>
    {(error || query.isError) && <Alert type="error" content={error || String(query.error)} />}
    <Text copyable>{task.task_id}</Text><Text>{sourceLabel(task)}</Text>
    {task.source.case_instance_id && <Text copyable>Case 实例 ID：{task.source.case_instance_id}</Text>}
    <Space wrap><Tag color={task.status === "fixed" ? "green" : "blue"}>{repairStatusLabel(task)}</Tag>
      <Text>阶段：{stageLabel(task.stage)}</Text><Text type="secondary">已用 {Math.round(task.elapsed_seconds ?? 0)} 秒 / {task.options.timeout_seconds} 秒</Text></Space>
    {task.message && <Alert type={task.status === "fixed" ? "success" : "info"} content={task.message} />}
    {task.source.kind === "robot_task" && <Text type="secondary">验证范围：冻结的本地复现测试和仓库单元回归；真实平台恢复需另行验证。</Text>}
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
      {["blocked", "interrupted", "cancelled"].includes(task.status) && !task.source.blockers?.length &&
        <Button loading={busy} onClick={() => void action("resume")}>检查并继续</Button>}
      <Button onClick={() => void query.refetch()}>刷新状态</Button>
      {task.source.test_sha256 && <a href={`/api/harness/tasks/${encodeURIComponent(taskId)}/reproducer`} download>下载冻结复现测试</a>}</Space>
    {task.source.diagnosis && <Alert type="info" title="复现依据" content={`${task.source.diagnosis.reason}；预期行为：${task.source.diagnosis.expected_behavior}`} />}
    {task.review_and_commit && <section><Text bold>AI 审核</Text>
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
    <Text bold>自检与复测记录</Text>
    {Object.keys(task.evaluations ?? {}).length ? <Table size="small" rowKey="phase" pagination={false} scroll={{ x: 620 }}
      data={Object.entries(task.evaluations ?? {}).map(([phase, value]) => ({ phase, ...value }))} columns={[
        { title: "阶段", render: (_, row) => stageLabel(row.phase) },
        { title: "验证记录", render: (_, row) => <Text copyable>{row.evaluation_id}</Text> },
        { title: "进度", render: (_, row) => row.complete ? `通过 ${row.passed_cases?.length ?? 0} / ${row.case_ids.length}` : "执行中" },
      ]} /> : <Empty description="完成来源自检后，将在这里显示复现和回归验证记录" />}
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
