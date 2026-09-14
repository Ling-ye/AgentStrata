import { Alert, Empty, Space, Table, Tag, Typography } from "@arco-design/web-react";
import { healthApi, type Coverage, type HealthGroup, type HealthTask } from "./api";

const labels: Record<string, string> = {
  pending: "未处理", running: "巡检中", completed: "已投递并完成巡检", unsupported: "尚未覆盖",
  preparing: "建立验证依据", coding: "修复中", accepted: "验收通过", failed: "未通过",
  interrupted: "本组未完成", needs_decision: "待判断", deferred: "等待依赖问题组",
};
export default function HealthLedger({ task }: { task: HealthTask }) {
  const counts = task.governance_summary;
  const groups = task.governance?.groups;
  const coverage = task.governance?.coverage;
  return <Space direction="vertical" size={16} style={{ width: "100%" }}>
    <Typography.Text>
      {counts?.coverage === "complete" ? "完成本轮范围巡检" : counts?.coverage === "partial" ?
        `范围巡检尚未完成：${counts.completed_batches}/${counts.total_batches} 批` : "历史覆盖信息：未知"}
      {counts?.coverage === "complete" && "；表示计划输入已处理，不保证找出了全部缺陷。"}
    </Typography.Text>
    {task.checkpoint && <Alert type={task.checkpoint_available ? "success" : "warning"} content={
      <Space wrap><span>已验收检查点 #{task.checkpoint.number} · {task.checkpoint.changed_files.length} 个累计变更文件</span>
        {task.checkpoint_available ? <a href={healthApi.candidateUrl(task.task_id)} download>下载已验证累计补丁</a> : <span>检查点缺失或摘要不一致，无法交付</span>}
      </Space>} />}
    {!!groups?.length && <details open><summary>问题分组 · {groups.length} 组</summary>
      <Table<HealthGroup> rowKey="id" data={groups} size="small" scroll={{ x: 620 }} pagination={{ pageSize: 10 }} columns={[
        { title: "根因与调用范围", dataIndex: "key" },
        { title: "问题数", render: (_, row) => row.finding_ids.length },
        { title: "进度", render: (_, row) => <Tag color={row.id === task.current_group ? "blue" : undefined}>{labels[row.status] ?? row.status}</Tag> },
        { title: "修复尝试", render: (_, row) => row.attempts.length },
      ]} expandedRowRender={row => <div>
        {task.governance?.findings.filter(f => row.finding_ids.includes(f.id)).map(f => <p key={f.id}>{f.summary} · {f.path}:{f.line}</p>)}
        <p>验证依据：{row.proof?.kind === "mechanical" ? "固定结构与 lint 检查" : row.proof?.diagnosis?.reason ?? "尚未建立"}</p>
        {row.proof?.diagnosis?.structural_before && <p>结构改进依据：{row.proof.diagnosis.structural_before}</p>}
        {row.proof?.sha256 && <p>冻结测试摘要：{row.proof.sha256}</p>}
        {row.reason && <p>原因：{row.reason}</p>}{row.stop_reason && <p>停止原因：{row.stop_reason}</p>}
        {!!row.depends_on.length && <p>依赖：{row.depends_on.join("、")}</p>}
        {row.attempts.map(number => {
          const attempt = task.attempts?.find(a => a.number === number);
          return attempt?.patch_sha256 ? <p key={number}><a href={healthApi.patchUrl(task.task_id, number)} download>下载候选 #{number} 的组内补丁</a></p> : null;
        })}
      </div>} />
    </details>}
    {coverage ? <details><summary>领域覆盖与实际输入 · {coverage.length} 批</summary>
      <Table<Coverage> rowKey="id" data={coverage} size="small" scroll={{ x: 540 }} pagination={{ pageSize: 10 }} columns={[
        { title: "领域", dataIndex: "area" }, { title: "文件块", render: (_, row) => row.blocks.length },
        { title: "状态", render: (_, row) => labels[row.status] ?? row.status },
      ]} expandedRowRender={row => <div><p>{row.summary ?? row.reason}</p>
        {row.blocks.map((block, i) => <p key={i}>{block.path}{block.start_line ? `:${block.start_line}–${block.end_line}` : ""}<br />摘要：{block.block_sha256 ?? block.sha256}</p>)}
      </div>} />
    </details> : !groups && <Empty description="旧记录保留原有发现与验证；未记录的覆盖和分组信息未知" />}
  </Space>;
}
