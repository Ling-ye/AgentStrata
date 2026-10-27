import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Card, Popconfirm, Select, Space, Table, Tag, Typography } from "@arco-design/web-react";
import { deliveryActive, type RepairTask } from "./api";
import { ACTIVATION_PENDING, activationLabel, comparisonRows, improvementApi, type Activation, type ActivationAction, type CheckFact } from "./improvements";

const { Text } = Typography;
const factsStyle = { maxHeight: 320, overflow: "auto", whiteSpace: "pre-wrap", overflowWrap: "anywhere" } as const;
function factLabel(fact?: CheckFact) {
  if (!fact) return "未记录";
  const outcome = ({ passed: "通过", failed: "失败", error: "执行错误", skipped: "未执行" } as Record<string, string>)[fact.outcome] ?? fact.outcome;
  return fact.failure_kind ? `${outcome} · ${fact.failure_kind}` : outcome;
}

export function ApplicationResult({ task }: { task: RepairTask }) {
  const comparison = useQuery({ queryKey: ["harness-comparison", task.task_id], queryFn: ({ signal }) => improvementApi.comparison(task.task_id, signal),
    retry: false, refetchInterval: deliveryActive(task) ? 2000 : false });
  const value = comparison.data;
  return <Space direction="vertical" size={16} style={{ width: "100%" }}>
    <Card title={task.source.task_type === "integrate" ? "外部能力接入" : "应用改进目标"}>
      <p style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{task.source.goal}</p>
      <Text type="secondary">应用：{task.source.bot_id} · 执行：{task.source.runtime_id ?? "见冻结条件"}</Text>
      <details><summary>允许修改的范围</summary><pre style={factsStyle}>{task.source.allowed_paths?.join("\n") || "未记录"}</pre></details>
    </Card>
    <Card title="基线与候选对照" extra={<Button size="small" onClick={() => void comparison.refetch()}>刷新对照</Button>}>
      {comparison.isError && <Alert type="error" content={String(comparison.error)} />}
      {value && <Space direction="vertical" size={12} style={{ width: "100%" }}>
        <Text>第 {value.attempt ?? "—"} 轮候选 · {value.runtime_id ?? "运行时未记录"}</Text>
        <Table size="small" rowKey="key" scroll={{ x: 660 }} pagination={false} data={comparisonRows(value)} noDataElement="尚无对照证据，等待验证阶段完成" columns={[
          { title: "检查", width: 240, render: (_, row) => <span style={{ overflowWrap: "anywhere" }}>{row.check} · 第 {row.repetition} 次 {row.primary && <Tag size="small">目标</Tag>}</span> },
          { title: "基线", render: (_, row) => factLabel(row.facts.reproduction) },
          { title: "候选", render: (_, row) => factLabel(row.facts.verification) },
          { title: "独立确认", render: (_, row) => factLabel(row.facts.confirmation) },
        ]} />
        <Alert type={value.accepted_candidate ? "success" : "info"} content={value.accepted_candidate ? "已签发独立接受回执；实例是否启用见下方版本状态。" : "尚无独立接受回执。单项通过不代表候选已经被接受。"} />
        {!!value.verification_gaps.length && <Alert type="warning" title="证据缺口" content={value.verification_gaps.map(item => item.message || item.code || item.requirement).join("；")} />}
        <details><summary>候选身份、审核与运行条件</summary>
          <pre style={factsStyle}>{JSON.stringify({ candidate_digest: value.candidate_digest, changed_files: value.changed_files,
            review: value.review, accepted_candidate: value.accepted_candidate, plan: value.plan,
            versions: Object.fromEntries(Object.entries(value.phases).map(([key, phase]) => [key, phase.versions ?? null])) }, null, 2)}</pre>
        </details>
        {!!value.changed_files.length && <a href={`/api/harness/tasks/${encodeURIComponent(task.task_id)}/candidate-patch`} download>下载候选补丁</a>}
      </Space>}
      {comparison.isPending && <Text>正在读取对照证据…</Text>}
    </Card>
    <ActivationPanel key={task.task_id} task={task} />
  </Space>;
}

function ActivationPanel({ task }: { task: RepairTask }) {
  const client = useQueryClient();
  const [target, setTarget] = useState(`instance:${task.source.bot_id}`);
  const [busy, setBusy] = useState<ActivationAction>();
  const [cancelling, setCancelling] = useState(false);
  const [error, setError] = useState("");
  const current = task.archived === false;
  const targets = useQuery({ queryKey: ["harness-applications"], queryFn: ({ signal }) => improvementApi.applications(signal), enabled: current, retry: false });
  const queryKey = ["harness-activation", task.task_id];
  const query = useQuery({ queryKey, queryFn: ({ signal }) => improvementApi.activation(task.task_id, signal),
    enabled: current, retry: false,
    refetchInterval: value => current && (busy || ACTIVATION_PENDING.includes(value.state.data?.state ?? "")) ? 2000 : false });
  const record = query.data;
  const accepted = current && task.status === "fixed" && task.candidate_available === true;
  async function change(action: ActivationAction) {
    if (!current || (action === "cancel" ? cancelling : busy || cancelling)) return;
    if (action === "cancel") setCancelling(true); else setBusy(action);
    setError("");
    try {
      const result = await improvementApi.changeActivation(task.task_id, action, action === "prepare" || action === "activate" ? record?.target_id || target : undefined);
      client.setQueryData<Activation | null>(queryKey, result);
    } catch (error) { setError(`${String(error)}。请刷新版本状态核对原操作；执行结果未知时使用对账。`); }
    finally { if (action === "cancel") setCancelling(false); else setBusy(undefined); void query.refetch(); }
  }
  return <Card title="实例版本与启用" extra={<Button size="small" disabled={!current} onClick={() => void query.refetch()}>刷新版本状态</Button>}>
    <Space direction="vertical" size={12} style={{ width: "100%" }}>
      {!current && <Alert type="info" content="旧协议任务只读归档，不能从历史验收记录启用版本。" />}
      {(error || query.isError) && <Alert type="error" content={error || String(query.error)} />}
      {query.isPending && current && <Text>正在读取版本状态…</Text>}
      {current && record === null && <Text type="secondary">尚未准备版本。候选验收通过后，选择登记实例或插件安装作为启用目标。</Text>}
      {record && <>
        <Space wrap><Tag>{activationLabel(record.state)}</Tag><Text>{record.target_id}</Text></Space>
        <Text copyable>候选版本：{record.desired_version}</Text>
        <Alert type={record.running_matches === true ? "success" : "warning"} content={record.running_matches === true ? "实际运行身份与本次操作记录一致。" : record.running_matches === false ? "实际运行身份与本次操作目标不一致；尚不能确认启用成功。" : "暂时无法确认实际运行版本，请检查实例状态。"} />
        {(record.error || record.recovery_error || record.message) && <Text>{[record.message, record.error, record.recovery_error].filter(Boolean).join("；")}</Text>}
        <details><summary>切换步骤与运行版本</summary><pre style={factsStyle}>{JSON.stringify({ steps: record.steps, running_versions: record.running_versions, restored: record.restored, cancel_requested: record.cancel_requested }, null, 2)}</pre></details>
      </>}
      {current && <>
        <Select aria-label="版本启用目标" value={record?.target_id || target} onChange={setTarget} disabled={!!busy || !!record}
          loading={targets.isPending} options={(targets.data?.activation_targets ?? []).filter(item => item.kind === "plugin" || item.bot_id === task.source.bot_id)
            .map(item => ({ value: item.target_id, label: `${item.display_name} · ${item.target_id}` }))} />
        {targets.isError && <Alert type="error" content={String(targets.error)} />}
        <Text type="secondary">插件安装需要与本次仓库应用的冻结配置、模型和基线配对；准备时会核对实际安装身份。</Text>
        <Space wrap>
          {(!record || ["cancelled", "rolled_back", "failed"].includes(record.state)) && <Button disabled={!accepted || !!busy || query.isPending || query.isError || !target.trim()} loading={busy === "prepare"} onClick={() => void change("prepare")}>准备候选版本</Button>}
          {record?.state === "prepared" && <Popconfirm title="启用后将等待在途任务收尾并切换所选实例，确认继续？" onOk={() => change("activate")}><Button type="primary" disabled={!accepted || !!busy} loading={busy === "activate"}>启用已验收版本</Button></Popconfirm>}
          {record?.state === "active" && <Popconfirm title="撤回至上一运行版本？已有外部动作和业务数据不会回退。" onOk={() => change("rollback")}><Button disabled={!!busy} loading={busy === "rollback"}>撤回版本</Button></Popconfirm>}
          {record && ACTIVATION_PENDING.includes(record.state) && <Button disabled={!!busy} loading={busy === "reconcile"} onClick={() => void change("reconcile")}>对账并恢复</Button>}
          {record && ["prepared", ...ACTIVATION_PENDING].includes(record.state) && <Button status="danger" disabled={cancelling} loading={cancelling} onClick={() => void change("cancel")}>取消启用</Button>}
        </Space>
        <Text type="secondary">版本启用独立于源码交付。运行身份核验确认代码与配置已加载；应用效果仍以实际评测和外部执行证据为准。</Text>
      </>}
    </Space>
  </Card>;
}
