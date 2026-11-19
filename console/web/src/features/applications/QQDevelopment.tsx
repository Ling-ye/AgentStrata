import ProjectTaskRecord from "./ProjectTaskRecord";
import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Card, Input, Space, Tag, Typography } from "@arco-design/web-react";
import { developmentApi, DevelopmentRequestError, developmentTaskLabel, sourceStateLabel, type DevelopmentTask } from "./developmentApi";
import { canonicalDevelopmentText, DESKTOP_REQUIREMENT_LIMIT, developmentTextLength } from "./desktopDevelopment";
import { usePendingApplicationRequest } from "./usePendingApplicationRequest";
const { Text, Paragraph } = Typography;
export default function QQDevelopment({ id, onDirty, mode = "develop" }: { id: string; onDirty?: (dirty: boolean) => void; mode?: "develop" | "records" | "versions" }) {
  const client = useQueryClient();
  const source = useQuery({ queryKey: ["application-development", id], queryFn: ({ signal }) => developmentApi.status(id, signal), retry: false });
  const tasks = useQuery({ queryKey: ["application-development-tasks", id], queryFn: ({ signal }) => developmentApi.tasks(id, signal), retry: false, refetchInterval: 5000 });
  const [goal, setGoal] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const { pending, restored, storageError, remember, acknowledge } = usePendingApplicationRequest(id, "qq-development");
  const original = useQuery({ queryKey: ["application-development-request", id, pending?.id],
    queryFn: ({ signal }) => developmentApi.request(id, pending!.id, signal), enabled: mode === "develop" && !!pending, retry: false, refetchInterval: 3000 });
  const [recovered, setRecovered] = useState<DevelopmentTask>();
  const recoveredTask = useQuery({ queryKey: ["application-development-task", id, recovered?.task_id],
    queryFn: ({ signal }) => developmentApi.task(id, recovered!.task_id, signal), enabled: !!recovered, retry: false, refetchInterval: 5000 });
  const gate = useRef(false);
  const alive = useRef(true);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  useEffect(() => { onDirty?.(!!goal || busy); }, [goal, busy, onDirty]);
  useEffect(() => {
    const result = original.data;
    if (!pending || result?.id !== pending.id || result.state !== "submitted" || !result.task ||
      result.task.request_id !== pending.id || result.goal !== canonicalDevelopmentText(pending.text)) return;
    try {
      if (acknowledge(pending)) {
        setRecovered(result.task); setGoal(""); setError("");
        setNotice("已找回原开发任务，未创建新请求；请核对任务状态和独立验收结果。");
        void client.invalidateQueries({ queryKey: ["application-development-tasks", id] });
      }
    } catch { /* The hook preserves the original request and displays the storage error. */ }
  }, [original.data, pending, acknowledge, client, id]);
  async function refresh() {
    await Promise.all([source.refetch(), tasks.refetch(),
      ...(pending ? [original.refetch()] : []),
      ...(recovered ? [recoveredTask.refetch()] : []),
      client.invalidateQueries({ queryKey: ["application-improvement-readiness", id] }),
      client.invalidateQueries({ queryKey: ["application", id] })]);
  }
  async function perform(operation: () => Promise<unknown>, success: string) {
    if (gate.current) return;
    gate.current = true;
    setBusy(true); setError(""); setNotice("");
    try { await operation(); if (alive.current) setNotice(success); }
    catch (value) { if (alive.current) setError(value instanceof Error ? value.message : "操作结果未确认，请先刷新核对"); }
    finally { await refresh(); gate.current = false; if (alive.current) setBusy(false); }
  }
  function prepare() {
    if (!window.confirm("准备当前应用的独立公开源码基线？请先停止应用。本操作不会调用模型、安装候选或启动 QQ。")) return;
    void perform(() => developmentApi.prepare(id), "源码已准备。下一步须明确提交目标，并经过独立验收。");
  }
  function start() {
    const text = canonicalDevelopmentText(goal.trim());
    if (!restored || storageError || gate.current || (!pending && (!ready || !text || developmentTextLength(text) > DESKTOP_REQUIREMENT_LIMIT)) ||
      !window.confirm(pending ? "使用原请求编号核对并继续此开发目标？不会另建需求；若原任务尚未登记，将使用开发和评测模型，可能产生费用。" : "为当前 QQ 应用启动提示词改进与独立测评？将使用开发和评测模型，可能产生费用。不会自动安装、启动应用或向 QQ 投递。")) return;
    const submitted = pending || { id: crypto.randomUUID().replace(/-/g, ""), text };
    void perform(async () => {
      remember(submitted);
      try {
        const task = await developmentApi.start(id, submitted.id, submitted.text);
        if (task.request_id !== submitted.id) throw new Error("尚未确认原请求，请刷新核对；不会另建需求。");
        const cleared = acknowledge(submitted);
        if (alive.current) { setRecovered(task); if (cleared) setGoal(""); }
      } catch (error) {
        if (error instanceof DevelopmentRequestError && error.status === 422) {
          if (acknowledge(submitted) && alive.current) setGoal(submitted.text);
        }
        throw error;
      }
    }, "开发请求已登记。请以任务状态和独立验收结果为准。");
  }
  function action(task: string, verb: "cancel" | "install") {
    if (gate.current || (verb === "install" && !!pending) || !window.confirm(verb === "install" ? "安装此任务已独立验收的版本，作为下次启动使用的源码？请先停止应用；凭据和业务数据保留，本操作不会启动 QQ。" : "请求取消这个开发任务？以服务器最终状态为准。")) return;
    void perform(() => developmentApi.action(id, task, verb), verb === "install" ? "验收版本已安装。应用仍未启动，真实模型与 QQ 收发尚待验证。" : "取消请求已提交，请核对任务最终状态。");
  }
  const ready = source.data && ["prepared", "installed"].includes(source.data.state) && source.data.definition_matches;
  const visibleTasks = tasks.data?.tasks || [];
  const outsideCurrentList = recovered && !visibleTasks.some(task => task.task_id === recovered.task_id);
  const extraTask = outsideCurrentList ? recoveredTask.data : undefined;
  const draftTooLong = developmentTextLength(canonicalDevelopmentText(goal.trim())) > DESKTOP_REQUIREMENT_LIMIT;
  return <Card title={mode === "develop" ? "提示词改进与受控交付" : mode === "versions" ? "版本与交付" : "本应用的 Harness 记录"}><Space direction="vertical" size={12} style={{ width: "100%" }}>
    <Alert type="info" content="这是独立的开发流程，不是配置对话。首版只改进此应用的身份与回复提示词，保持渠道、权限、模型引用和框架代码不变。必须先停止应用；独立验收通过后再明确安装。" />
    {source.data && <><Tag>{sourceStateLabel(source.data.state)}</Tag><Text>{source.data.message}</Text>{source.data.source_digest && <Text type="secondary">当前源码：{source.data.source_digest}</Text>}</>}
    {(source.isError || tasks.isError) && <Alert type="warning" content="当前开发状态暂不可用。不会回退到其他应用或全局开发记录，请刷新核对。" />}
    {mode === "develop" && <><Space wrap><Button disabled={busy} loading={source.isFetching || tasks.isFetching || original.isFetching} onClick={() => void refresh()}>刷新开发状态</Button><Button disabled={busy || !!pending || source.isPending || ready} onClick={prepare}>准备当前源码</Button><Button href="#models">模型与开发账号</Button></Space>
    {error && <Alert type="warning" content={error} />}{notice && <Alert type="info" content={notice} />}
    {storageError && <Alert type="error" content={storageError} />}
    {pending && <Alert type="warning" title="正在核对原开发请求" content={<><Paragraph className="application-prose">{pending.text}</Paragraph><Text>刷新只查询原请求，不调用模型。需要继续时使用同一编号，不会换目标或源码版本。</Text></>} />}
    {pending && original.isError && <Alert type="warning" content="原请求暂无法核对，请检查连接后刷新；已保留编号，不会自动重发或创建其他任务。" />}
    {outsideCurrentList && recoveredTask.isError && <Alert type="warning" content="原请求已登记，但其历史任务状态暂无法核对。请刷新；不会将缓存状态作为当前验收或安装依据。" />}
    <Input.TextArea aria-label="提示词改进目标" value={goal} onChange={setGoal} disabled={busy || !restored || !!storageError || !!pending} autoSize={{ minRows: 3, maxRows: 8 }} placeholder="具体描述希望改变的回复行为与验收要求，不要输入凭据" />
    {draftTooLong && <Alert type="warning" content={`目标最多 ${DESKTOP_REQUIREMENT_LIMIT} 个字符，内容已保留，请精简后发送。`} />}
    <Button type="primary" disabled={busy || !restored || !!storageError || (!pending && (!ready || !goal.trim() || draftTooLong))} loading={busy} onClick={start}>{pending ? "核对并重试原开发请求" : "启动提示词改进与测评"}</Button></>}
    {[...(extraTask ? [extraTask] : []), ...visibleTasks].filter(task => mode !== "versions" || task.can_install).map(task => <Card key={task.task_id} size="small"><Space direction="vertical"><Text>{task.task_id}</Text><Tag>{developmentTaskLabel(task.status)}</Tag>
      <ProjectTaskRecord id={id} task={task.task_id} active={task.can_cancel} />
      {extraTask?.task_id === task.task_id && <Text type="secondary">原请求对应任务；可能属于此前源码基线，以核对结果为准。</Text>}
      {task.can_install && mode !== "records" ? <Button disabled={busy || !!pending} onClick={() => action(task.task_id, "install")}>安装此验收版本</Button> : task.can_cancel && <Button disabled={busy} onClick={() => action(task.task_id, "cancel")}>请求取消任务</Button>}
    </Space></Card>)}
    {mode !== "develop" && (error || notice) && <Alert type={error ? "warning" : "info"} content={error || notice} />}
    <Text type="secondary">显示当前基线最近 20 项开发记录。版本切换后旧证据仍保留，不混入其他应用。静态检查或模拟测评不代表真实 QQ 已完成交付。</Text>
  </Space></Card>;
}
