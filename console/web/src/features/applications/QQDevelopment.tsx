import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Card, Input, Space, Tag, Typography } from "@arco-design/web-react";
import { developmentApi, developmentTaskLabel, sourceStateLabel } from "./developmentApi";
const { Text } = Typography;
export default function QQDevelopment({ id, onDirty }: { id: string; onDirty: (dirty: boolean) => void }) {
  const client = useQueryClient();
  const source = useQuery({ queryKey: ["application-development", id], queryFn: ({ signal }) => developmentApi.status(id, signal), retry: false });
  const tasks = useQuery({ queryKey: ["application-development-tasks", id], queryFn: ({ signal }) => developmentApi.tasks(id, signal), retry: false, refetchInterval: 5000 });
  const [goal, setGoal] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const request = useRef<{ id: string; goal: string }>();
  const alive = useRef(true);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  useEffect(() => { onDirty(!!goal || busy); }, [goal, busy, onDirty]);
  async function refresh() {
    await Promise.all([source.refetch(), tasks.refetch(),
      client.invalidateQueries({ queryKey: ["application-improvement-readiness", id] }),
      client.invalidateQueries({ queryKey: ["application", id] })]);
  }
  async function perform(operation: () => Promise<unknown>, success: string) {
    if (busy) return;
    setBusy(true); setError(""); setNotice("");
    try { await operation(); if (alive.current) setNotice(success); }
    catch (value) { if (alive.current) setError(value instanceof Error ? value.message : "操作结果未确认，请先刷新核对"); }
    finally { await refresh(); if (alive.current) setBusy(false); }
  }
  function prepare() {
    if (!window.confirm("准备当前应用的独立公开源码基线？请先停止应用。本操作不会调用模型、安装候选或启动 QQ。")) return;
    void perform(() => developmentApi.prepare(id), "源码已准备。下一步须明确提交目标，并经过独立验收。");
  }
  function start() {
    if (!goal.trim() || busy || !window.confirm("为当前 QQ 应用启动提示词改进与独立测评？将使用开发和评测模型，可能产生费用。不会自动安装、启动应用或向 QQ 投递。")) return;
    if (!request.current || request.current.goal !== goal.trim()) request.current = { id: crypto.randomUUID(), goal: goal.trim() };
    const submitted = request.current;
    void perform(async () => { await developmentApi.start(id, submitted.id, submitted.goal); if (alive.current) { setGoal(""); request.current = undefined; } }, "开发请求已登记。请以任务状态和独立验收结果为准。");
  }
  function action(task: string, verb: "cancel" | "install") {
    if (busy || !window.confirm(verb === "install" ? "安装此任务已独立验收的版本，作为下次启动使用的源码？请先停止应用；凭据和业务数据保留，本操作不会启动 QQ。" : "请求取消这个开发任务？以服务器最终状态为准。")) return;
    void perform(() => developmentApi.action(id, task, verb), verb === "install" ? "验收版本已安装。应用仍未启动，真实模型与 QQ 收发尚待验证。" : "取消请求已提交，请核对任务最终状态。");
  }
  const ready = source.data && ["prepared", "installed"].includes(source.data.state) && source.data.definition_matches;
  return <Card title="提示词改进与受控交付"><Space direction="vertical" size={12} style={{ width: "100%" }}>
    <Alert type="info" content="这是独立的开发流程，不是配置对话。首版只改进此应用的身份与回复提示词，保持渠道、权限、模型引用和框架代码不变。必须先停止应用；独立验收通过后再明确安装。" />
    {source.data && <><Tag>{sourceStateLabel(source.data.state)}</Tag><Text>{source.data.message}</Text>{source.data.source_digest && <Text type="secondary">当前源码：{source.data.source_digest}</Text>}</>}
    {(source.isError || tasks.isError) && <Alert type="warning" content="当前开发状态暂不可用。不会回退到其他应用或全局开发记录，请刷新核对。" />}
    <Space wrap><Button disabled={busy} loading={source.isFetching || tasks.isFetching} onClick={() => void refresh()}>刷新开发状态</Button><Button disabled={busy || source.isPending || ready} onClick={prepare}>准备当前源码</Button></Space>
    {error && <Alert type="warning" content={error} />}{notice && <Alert type="info" content={notice} />}
    <Input.TextArea aria-label="提示词改进目标" value={goal} onChange={setGoal} disabled={busy} autoSize={{ minRows: 3, maxRows: 8 }} placeholder="具体描述希望改变的回复行为与验收要求，不要输入凭据" />
    <Button type="primary" disabled={busy || !ready || !goal.trim()} loading={busy} onClick={start}>启动提示词改进与测评</Button>
    {tasks.data?.tasks.map(task => <Card key={task.task_id} size="small"><Space direction="vertical"><Text>{task.task_id}</Text><Tag>{developmentTaskLabel(task.status)}</Tag>
      {task.can_install ? <Button disabled={busy} onClick={() => action(task.task_id, "install")}>安装此验收版本</Button> : task.can_cancel && <Button disabled={busy} onClick={() => action(task.task_id, "cancel")}>请求取消任务</Button>}
    </Space></Card>)}
    <Text type="secondary">显示当前基线最近 20 项开发记录。版本切换后旧证据仍保留，不混入其他应用。静态检查或模拟测评不代表真实 QQ 已完成交付。</Text>
  </Space></Card>;
}
