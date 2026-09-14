import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Empty, Space, Spin, Tag, Typography } from "@arco-design/web-react";
import { ACTIVE, harnessApi, repairStatusLabel, stageLabel, type ProgressTask } from "./api";
import { heartbeatStatus, progressSourceLabel, repairRoundLabel } from "./progress";
import "./progress.css";

const { Text } = Typography;

export function RepairProgress({ task, refreshTask, title = "修复进度", statusLabel }: {
  task: ProgressTask; refreshTask: () => Promise<unknown>; title?: string; statusLabel?: string;
}) {
  const client = useQueryClient();
  const active = ACTIVE.includes(task.status);
  const wasActive = useRef(active);
  const [now, setNow] = useState(Date.now);
  const query = useQuery({ queryKey: ["harness-progress", task.task_id],
    queryFn: ({ signal }) => harnessApi.progress(task.task_id, signal), retry: false,
    refetchInterval: active ? 2000 : false });
  useEffect(() => {
    if (wasActive.current && !active) {
      // Cancel a poll that may have started before the terminal task snapshot,
      // then fetch once more to include the worker's final flushed output.
      const queryKey = ["harness-progress", task.task_id];
      void client.cancelQueries({ queryKey, exact: true }).then(() =>
        client.invalidateQueries({ queryKey, exact: true }));
    }
    wasActive.current = active;
  }, [active, task.task_id, client]);
  useEffect(() => {
    setNow(Date.now());
    if (!active) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [active, task.heartbeat_at]);
  const heartbeat = heartbeatStatus(task, now);
  const round = repairRoundLabel(task);
  const data = query.data;
  return <section className="repair-progress" aria-label={title}>
    <Space wrap><Text bold>{title}</Text>
      <Button size="small" loading={query.isFetching} onClick={() => {
        setNow(Date.now());
        void Promise.all([refreshTask(), query.refetch()]);
      }}>刷新进度</Button></Space>
    <Space wrap><Tag color={task.status === "fixed" ? "green" : "blue"}>{statusLabel ?? repairStatusLabel(task)}</Tag>
      <Text>阶段：{stageLabel(task.stage)}</Text>{round && <Text>{round}</Text>}
      <Text type="secondary">{task.elapsed_seconds == null ? "用时尚未记录" : `已用 ${Math.round(task.elapsed_seconds)} 秒`} / {task.options.timeout_seconds} 秒</Text>
      <Text type="secondary">{heartbeat.label}</Text></Space>
    {heartbeat.stale && <Alert type="warning" content="心跳暂未更新；可刷新确认，任务状态以实际执行结果为准。" />}
    {query.isError && <Alert type="error" content={`进度读取失败：${String(query.error)}。已显示的内容保留，可刷新重试。`} />}
    {data?.message && <Alert type="warning" content={data.message} />}
    {data?.source && <Space wrap><Text>动态来源：{progressSourceLabel(data.source)}</Text>
      {!data.source.current && <Text type="secondary">其他阶段的最近记录，并非当前动作</Text>}
      {data.updated_at != null && <Text type="secondary">日志更新：{new Date(data.updated_at * 1000).toLocaleString()}</Text>}</Space>}
    <Text type="secondary">公开消息和命令完成后更新；等待期间可查看最近心跳。</Text>
    {query.isPending ? <Spin tip="读取公开执行动态…" /> : !data?.events.length ?
      <Empty description={query.isError ? "暂无可显示的公开执行记录" : "尚无公开执行输出"} /> :
      <ol className="repair-progress-events" aria-label="最近公开动态">
        {data.events.map(event => <li key={event.id}>
          {event.type === "agent_message" ? <><Text bold>公开消息</Text><pre>{event.text}</pre></> :
            <><Space wrap><Text bold>已完成命令</Text><Tag size="small" color={event.exit_code === 0 ? "green" : "orange"}>
              {event.exit_code == null ? "退出状态未记录" : `退出码 ${event.exit_code}`}</Tag></Space><pre>{event.command}</pre>
              <details><summary>命令输出</summary><pre>{event.aggregated_output || "没有公开输出"}</pre></details></>}
          {event.truncated && <Text type="secondary">此条内容过长，仅展示摘要。</Text>}
        </li>)}
      </ol>}
    {data?.truncated && <Text type="secondary">仅展示最近记录及有界正文；完整执行归档可在执行结束后查看。</Text>}
  </section>;
}
