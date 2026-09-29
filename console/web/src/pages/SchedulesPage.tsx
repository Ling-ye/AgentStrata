import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Empty, Message, Popconfirm, Select, Space, Spin, Tag, Typography } from "@arco-design/web-react";
import { api } from "../api";
import { schedulesApi } from "../features/schedules/api";
import { activeRun, displayTime, runLabels, scheduleLabel, type ScheduleSettings, type ScheduleTask } from "../features/schedules/model";
import ScheduleEditor from "../features/schedules/ScheduleEditor";
import RunDetail from "../features/schedules/RunDetail";
import PageSection from "../shared/ui/PageSection";
import "../features/schedules/schedules.css";

export default function SchedulesPage() {
  const bots = useQuery({ queryKey: ["bots"], queryFn: api.listBots, retry: false });
  const [selected, setSelected] = useState("");
  const supported = bots.data?.filter(bot => bot.runtime_kind === "gateway" && bot.platform === "qq") ?? [];
  const bot = supported.find(bot => bot.instance_id === selected) ?? supported[0];
  const [editing, setEditing] = useState(false);
  return <div className="schedules-page">
    <PageSection title="机器人定时任务" description="告诉 AI 定期调查什么，查看每次报告和 QQ 群投递结果。"
      extra={<Select aria-label="定时任务机器人" disabled={editing} value={bot?.instance_id} style={{ width: "min(300px, 70vw)" }}
        onChange={setSelected} options={supported.map(bot => ({ label: bot.display_name || bot.instance_id, value: bot.instance_id }))} />}>
      {bots.isLoading && <Spin tip="读取机器人…" />}
      {bots.error && <Alert type="error" content={bots.error.message} action={<Button onClick={() => void bots.refetch()}>重试</Button>} />}
      {!bots.isLoading && !bots.error && !bot && <Empty description="请先配置使用原生 QQ Gateway 的机器人" />}
      {bot && <BotSchedules key={bot.instance_id} bot={bot.instance_id} onEditing={setEditing} />}
    </PageSection>
  </div>;
}

function BotSchedules({ bot, onEditing }: { bot: string; onEditing: (value: boolean) => void }) {
  const client = useQueryClient();
  const [taskId, setTaskId] = useState("");
  const [offset, setOffset] = useState(0);
  const [runId, setRunId] = useState("");
  const [editor, setEditor] = useState<{ task: ScheduleTask | null } | null>(null);
  const [error, setError] = useState("");
  const pendingRequest = useRef({ key: "", id: "" });
  const overview = useQuery({ queryKey: ["schedules", bot, "overview"],
    queryFn: ({ signal }) => schedulesApi.overview(bot, signal), retry: false, refetchInterval: 5000 });
  const history = useQuery({ queryKey: ["schedules", bot, "history", taskId, offset],
    queryFn: ({ signal }) => schedulesApi.history(bot, taskId, offset, signal), retry: false,
    enabled: !!overview.data, refetchInterval: 5000 });
  const mutation = useMutation({ mutationFn: (action: () => Promise<unknown>) => action(),
    onSuccess: async () => { await client.invalidateQueries({ queryKey: ["schedules", bot] }); },
    onError: (error: Error) => setError(error.message) });
  const busy = mutation.isPending;
  function act(action: () => Promise<unknown>) { setError(""); mutation.mutate(action); }
  function openEditor(task: ScheduleTask | null) { setError(""); setEditor({ task }); onEditing(true); }
  function closeEditor() { setEditor(null); onEditing(false); setError(""); }
  function save(settings: ScheduleSettings, requestId: string) {
    act(async () => {
      const task = editor?.task;
      const saved = task ? await schedulesApi.update(bot, task, settings) : await schedulesApi.create(bot, settings, requestId);
      setTaskId(saved.id); setOffset(0); closeEditor(); Message.success("定时任务已保存");
    });
  }
  function start(task: ScheduleTask, preview: boolean) {
    act(async () => {
      const key = `${task.id}:${task.revision}:${preview}`;
      if (pendingRequest.current.key !== key) pendingRequest.current = { key, id: crypto.randomUUID() };
      const run = await schedulesApi.start(bot, task, preview, pendingRequest.current.id);
      pendingRequest.current = { key: "", id: "" };
      setRunId(run.id); setOffset(0);
    });
  }
  const selected = overview.data?.tasks.find(task => task.id === taskId);
  return <div className="schedule-workbench">
    {overview.isLoading && <Spin tip="读取定时任务…" />}
    {overview.error && <Alert type="error" content={overview.error.message} action={<Button onClick={() => void overview.refetch()}>重试</Button>} />}
    {overview.data && <>
      <div className="schedule-toolbar"><Space wrap>
        <Tag color={overview.data.host.available ? "green" : "orange"}>{overview.data.host.available ? "调度运行中" : "调度未就绪"}</Tag>
        <Typography.Text type="secondary">{overview.data.tasks.length} 个任务 · 随机器人运行，关闭网页不影响执行</Typography.Text>
      </Space><Button type="primary" disabled={busy} onClick={() => openEditor(null)}>新建定时任务</Button></div>
      {!overview.data.host.available && <Alert type="warning" content="尚未收到机器人的有效调度心跳。配置可以保存，运行请求会等待机器人启动；请确认机器人已更新并启动。" />}
      {error && !editor && <Alert type="error" content={error} />}
      <div className="schedule-columns">
        <section className="schedule-list" aria-label="定时任务列表">
          <div className="schedule-section-heading"><strong>已配置的任务</strong><Button size="small" type="text" onClick={() => { setTaskId(""); setOffset(0); }}>全部运行记录</Button></div>
          {!overview.data.tasks.length && <Empty description="还没有定时任务。新建任务后，可以先预览一次报告。" />}
          {overview.data.tasks.map(task => <article key={task.id} className={`schedule-card ${taskId === task.id ? "selected" : ""}`}>
            <button className="schedule-task-select" onClick={() => { setTaskId(task.id); setOffset(0); }}>
              <strong>{task.settings.name}</strong><Tag color={task.settings.enabled ? "green" : "gray"}>{task.settings.enabled ? "已启用" : "已暂停"}</Tag>
            </button>
            <p className="schedule-muted">{scheduleLabel(task.settings)}</p>
            <p>QQ 群：{task.settings.group_id}</p>
            <p className="schedule-instruction" title={task.settings.instruction}>{task.settings.instruction}</p>
            <div className="schedule-meta">下次：{task.next_run ? displayTime(task.next_run, task.settings.timezone) : "暂停中"}</div>
            <div className="schedule-meta">最近：{task.last_run ? `${runLabels[task.last_run.status] ?? task.last_run.status} · ${displayTime(task.last_run.created_at, task.settings.timezone)}` : "尚未运行"}</div>
            <Space wrap size={4} className="schedule-actions">
              <Button size="small" disabled={busy || activeRun(task.active_run ?? undefined)} onClick={() => start(task, true)}>预览运行</Button>
              <Popconfirm title={`现在调查并推送到 QQ 群 ${task.settings.group_id}？`} content="将创建一次真实推送；请先核对群号和任务要求。"
                onOk={() => start(task, false)}><Button size="small" disabled={busy || activeRun(task.active_run ?? undefined)}>立即推送</Button></Popconfirm>
              <Button size="small" type="text" disabled={busy} onClick={() => openEditor(task)}>编辑</Button>
              <Button size="small" type="text" disabled={busy} onClick={() => act(() => schedulesApi.update(bot, task, { ...task.settings, enabled: !task.settings.enabled }))}>
                {task.settings.enabled ? "暂停" : "启用"}</Button>
              <Popconfirm title="删除定时任务？" content="历史运行记录仍会保留。" onOk={() => act(async () => {
                await schedulesApi.remove(bot, task); if (taskId === task.id) { setTaskId(""); setOffset(0); }
              })}><Button size="small" type="text" status="danger" disabled={busy || activeRun(task.active_run ?? undefined)}>删除</Button></Popconfirm>
            </Space>
          </article>)}
        </section>
        <section className="schedule-history" aria-label="执行历史">
          <div className="schedule-section-heading"><strong>{selected ? `${selected.settings.name} · 运行记录` : "全部运行记录"}</strong></div>
          {history.isLoading && <Spin tip="读取执行历史…" />}
          {history.error && <Alert type="error" content={history.error.message} action={<Button onClick={() => void history.refetch()}>重试</Button>} />}
          {history.data?.runs.length === 0 && <Empty description="暂无执行记录" />}
          {history.data?.runs.map(run => <button key={run.id} className="schedule-run-row" onClick={() => setRunId(run.id)}>
            <div><strong>{run.settings.name}</strong><small>{displayTime(run.scheduled_for, run.settings.timezone)} · {run.preview ? "仅预览" : "QQ 群推送"}</small>
              <small>群 {run.settings.group_id} · {run.trigger === "timer" ? "定时" : "手动"}</small></div>
            <Tag color={run.status === "delivered" || run.status === "previewed" ? "green" : run.status === "failed" || run.status === "delivery_unknown" ? "orange" : "gray"}>
              {runLabels[run.status] ?? run.status}</Tag>
          </button>)}
          <div className="schedule-pagination"><Button size="small" disabled={offset === 0 || history.isFetching} onClick={() => setOffset(Math.max(0, offset - 20))}>上一页</Button>
            <span>第 {Math.floor(offset / 20) + 1} 页</span><Button size="small" disabled={history.data?.next_offset == null || history.isFetching}
              onClick={() => setOffset(history.data?.next_offset ?? offset)}>下一页</Button></div>
        </section>
      </div>
    </>}
    {editor && <ScheduleEditor key={editor.task?.id ?? "new"} task={editor.task} busy={busy} error={error} onSave={save} onClose={closeEditor} />}
    {runId && <RunDetail key={runId} bot={bot} id={runId} onClose={() => setRunId("")} busy={busy}
      onCancel={id => act(() => schedulesApi.cancel(bot, id))} />}
  </div>;
}
