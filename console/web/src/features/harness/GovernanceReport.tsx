import { useQuery } from "@tanstack/react-query";
import { Alert, Card, Space, Spin, Tag, Typography } from "@arco-design/web-react";
import { governanceApi } from "./governance";

export function GovernanceReport({ taskId, active }: { taskId: string; active: boolean }) {
  const query = useQuery({ queryKey: ["governance-report", taskId],
    queryFn: ({ signal }) => governanceApi.report(taskId, signal),
    retry: false, refetchInterval: active ? 5000 : false });
  if (query.isPending) return <Spin tip="读取治理依据…" />;
  if (query.isError) return <Alert type="error" content={String(query.error)} />;
  const report = query.data.report;
  if (!report) return <Alert type="info" content={active ? "正在调查仓库，治理发现尚未形成。" : "任务已停止，未形成完整调查报告；未覆盖范围尚未确认。"} />;
  return <Card title="治理依据与调查范围" style={{ minWidth: 0 }}>
    <Space direction="vertical" style={{ width: "100%", overflowWrap: "anywhere" }}>
      <Typography.Text>{report.summary}</Typography.Text>
      <Typography.Text type="secondary">Agent 报告阅读 {report.inspected_paths.length} 个路径，冻结索引共 {report.inventory_count} 个文件；宿主核实了 {report.evidence_receipts.length} 处源码证据。</Typography.Text>
      {!report.inspection_complete && <Alert type="info" content="这份报告不代表全仓已检查完毕。阅读范围可结合本任务命令日志核对。" />}
      {report.findings.map(finding => <Card key={finding.id} size="small" style={{ width: "100%" }}
        title={<Space wrap><Typography.Text bold>{finding.summary}</Typography.Text>
          <Tag>{finding.id === report.selected_finding_id ? "本次主题" : "其他发现"}</Tag>
          {finding.disposition === "needs_decision" && <Tag color="orange">需要判断</Tag>}</Space>}>
        <p>{finding.impact}</p>
        <p>规则依据：{finding.principle_refs.join("、")}</p>
        <p>涉及文件：{finding.affected_paths.join("、")}</p>
        <ul>{finding.acceptance_criteria.map((criterion, index) => <li key={index}>{criterion}</li>)}</ul>
        <details><summary>源码证据</summary>{finding.evidence.map((item, index) => <div key={index}>
          <Typography.Text code>{item.path}</Typography.Text>
          <pre style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere", maxHeight: 260, overflow: "auto" }}>{item.excerpt}</pre>
        </div>)}</details>
      </Card>)}
      {!!report.unresolved.length && <Alert type="warning" title="未决项" content={report.unresolved.join("；")} />}
      <details><summary>阅读路径与未覆盖范围</summary>
        <p>报告已读：{report.inspected_paths.join("、") || "未记录"}</p>
        <p>未覆盖／不确定：{report.uninspected.join("；") || "未另行说明，请结合证据判断"}</p>
      </details>
    </Space>
  </Card>;
}
