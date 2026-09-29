import { useQuery } from "@tanstack/react-query";
import { Alert, Button, Descriptions, Drawer, Empty, Space, Spin, Tag, Typography } from "@arco-design/web-react";
import { schedulesApi } from "./api";
import { activeRun, displayTime, errorLabels, runLabels, type ScheduleRun } from "./model";

export function RunSteps({ run }: { run: ScheduleRun }) {
  const stopped = !activeRun(run);
  const steps = [
    { title: "1. 触发", time: run.created_at, result: `${run.trigger === "timer" ? "定时触发" : "手动运行"} · ${run.preview ? "仅预览" : "推送 QQ 群"}` },
    { title: "2. 调查与总结", time: run.generated_at ?? run.started_at,
      result: run.generated_at ? "报告已生成" : run.status === "researching" ? "AI 正在调查" : stopped ? "未完成" : "等待执行" },
    { title: "3. 群消息投递", time: run.finished_at ?? run.delivery_started_at,
      result: run.preview ? "预览不发送" : run.generated_at ? (runLabels[run.status] ?? run.status) : "尚未发送" },
  ];
  return <ol className="schedule-steps">{steps.map(step => <li key={step.title}>
    <strong>{step.title}</strong><span>{step.result}</span><small>{displayTime(step.time, run.settings.timezone)}</small>
  </li>)}</ol>;
}

export default function RunDetail({ bot, id, onClose, onCancel, busy }: {
  bot: string; id: string; onClose: () => void; onCancel: (id: string) => void; busy: boolean;
}) {
  const query = useQuery({ queryKey: ["schedules", bot, "run", id], queryFn: ({ signal }) => schedulesApi.detail(bot, id, signal),
    retry: false, refetchInterval: query => activeRun(query.state.data) ? 2000 : false });
  const run = query.data;
  return <Drawer visible width="min(860px, 100vw)" title="定时任务运行记录" footer={null} onCancel={onClose} className="schedule-detail">
    {query.error && <Alert type="error" content={query.error.message} action={<Button onClick={() => void query.refetch()}>重试</Button>} />}
    {query.isLoading && <Spin tip="读取运行记录…" />}
    {run ? <Space direction="vertical" size={20} style={{ width: "100%" }}>
      <Space wrap><Typography.Title heading={5} style={{ margin: 0 }}>{run.settings.name}</Typography.Title><Tag>{runLabels[run.status] ?? run.status}</Tag>
        {activeRun(run) && <Button size="small" disabled={run.cancel_requested} loading={busy} onClick={() => onCancel(run.id)}>{run.cancel_requested ? "正在停止" : "停止本次运行"}</Button>}</Space>
      <RunSteps run={run} />
      {run.error_code && <Alert type={run.status === "delivery_unknown" ? "warning" : "info"} content={errorLabels[run.error_code] ?? `本次执行异常：${run.error_code}`} />}
      {run.status === "delivery_unknown" && <Alert type="warning" content="平台是否收到消息尚不确定，系统不会自动重发。请先查看群消息，再决定是否发起新的推送。" />}
      <Descriptions column={1} data={[
        { label: "目标群", value: run.settings.group_id },
        { label: "计划时间", value: `${displayTime(run.scheduled_for, run.settings.timezone)} · ${run.settings.timezone}` },
        { label: "昨天的范围", value: `${run.window_start} ≤ 时间 < ${run.window_end}` },
        { label: "配置版本", value: String(run.revision) },
      ]} />
      <div><Typography.Title heading={6}>本次调查要求</Typography.Title><pre className="schedule-prose">{run.settings.instruction}</pre></div>
      <div><Typography.Title heading={6}>{run.preview ? "预览报告" : "AI 报告"}</Typography.Title>
        {run.result_text ? <pre className="schedule-prose">{run.result_text}</pre> : <Empty description={activeRun(run) ? "报告尚未生成" : "本轮没有留存报告"} />}</div>
      <div><Typography.Title heading={6}>投递证据</Typography.Title>
        {run.receipts?.length ? run.receipts.map((receipt, index) => <div className="schedule-receipt" key={index}>
          <Tag>{receipt.stage === "provider_acknowledged" ? "QQ 平台已确认" : receipt.stage}</Tag>
          <span>{displayTime(receipt.observed_at, run.settings.timezone)}</span>
          {receipt.provider_message_id && <span>消息 ID：{receipt.provider_message_id}</span>}
        </div>) : <Typography.Text type="secondary">{run.preview ? "预览模式没有 QQ 投递回执" : "尚无平台投递回执"}</Typography.Text>}
        <p className="schedule-muted">平台确认表示 OneBot 已返回消息回执，不代表群成员已读。</p>
      </div>
      <a href={`#bots?instance=${encodeURIComponent(bot)}&tab=tasks&run=${encodeURIComponent(run.gateway_run_id)}`}>查看机器人执行轨迹</a>
      <details><summary>本次完整输入</summary><pre className="schedule-prose">{run.prompt}</pre></details>
    </Space> : !query.isLoading && !query.error && <Empty description="运行记录不存在" />}
  </Drawer>;
}
