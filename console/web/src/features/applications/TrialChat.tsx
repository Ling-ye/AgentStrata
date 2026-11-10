import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Card, Empty, Input, Space, Spin, Tag, Typography } from "@arco-design/web-react";
import { applicationsApi, type TrialChat as ChatState } from "./api";
const { Text, Paragraph } = Typography;
export const TRIAL_ACTIVE = ["queued", "unknown", "accepted", "running", "abort_requested", "recovery_required"];
export function trialStateLabel(state: string) { return ({ queued: "等待处理", unknown: "结果未知，请先核对", accepted: "已接收", running: "正在回复", abort_requested: "正在请求停止", recovery_required: "运行中断，需要检查", completed: "已回复", aborted: "已停止", failed: "回复失败" } as Record<string,string>)[state] || state; }
export default function TrialChat({ id, onDirty }: { id: string; onDirty: (dirty: boolean) => void }) {
  const client = useQueryClient();
  const query = useQuery({ queryKey: ["application-trial", id], queryFn: ({ signal }) => applicationsApi.chat(id, signal), retry: false, refetchInterval: 2500 });
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [uncertain, setUncertain] = useState<{ id: string; text: string }>();
  const alive = useRef(true);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  useEffect(() => { onDirty(!!text || busy || !!uncertain); }, [text, busy, uncertain, onDirty]);
  useEffect(() => { if (uncertain && query.data?.messages.some(message => message.id === uncertain.id)) { setUncertain(undefined); setText(""); setError(""); } }, [query.data, uncertain]);
  const active = query.data?.messages.some(message => TRIAL_ACTIVE.includes(message.state));
  async function send(retry = false) {
    if (busy || (!retry && (active || uncertain)) || !query.data?.available) return;
    const request = retry ? uncertain : { id: crypto.randomUUID(), text: text.trim() };
    if (!request?.text) return;
    setBusy(true); setError(""); setUncertain(request);
    try {
      const result = await applicationsApi.sendChat(id, request.id, request.text);
      client.setQueryData(["application-trial", id], result);
      if (alive.current) { setUncertain(undefined); setText(""); }
    } catch { if (alive.current) setError("提交结果尚未确认。先刷新核对，不要另发一条；可用原请求编号恢复。"); }
    finally { if (alive.current) setBusy(false); }
  }
  async function action(message: string, verb: "retry" | "abort") {
    if (busy) return;
    setBusy(true); setError("");
    try { const result: ChatState = await applicationsApi.chatAction(id, message, verb); client.setQueryData(["application-trial", id], result); }
    catch { if (alive.current) setError("操作结果尚未确认，请刷新核对原请求状态。关闭页面不会自动停止模型任务。"); }
    finally { if (alive.current) setBusy(false); }
  }
  return <Card title="应用试聊"><Space direction="vertical" size={16} style={{ width: "100%" }}>
    <Alert type="info" content="这是当前应用的独立试聊会话，不是管理对话，也不会发送到真实 QQ。试聊仅使用受限的应用回复能力，不能执行开发、读写文件或管理其他应用。模型调用可能产生费用。" />
    {!query.data?.available && <Alert type="warning" content={query.data?.reason || "正在核对试聊可用性"} />}
    {query.isError && <Alert type="error" content="无法读取试聊记录，请检查连接后刷新。不会自动重发。" />}
    <Button onClick={() => void query.refetch()} loading={query.isFetching}>刷新核对状态</Button>
    <div className="application-conversation">{query.isPending ? <Spin /> : !query.data?.messages.length ? <Empty description="还没有试聊记录" /> : query.data.messages.map(message => <div className="application-message" key={message.id}>
      <Text bold>你</Text><Paragraph className="application-prose">{message.text}</Paragraph><Tag>{trialStateLabel(message.state)}</Tag>
      {message.reply && <Paragraph className="application-prose">{message.reply}</Paragraph>}
      {message.state === "recovery_required" && <Alert type="warning" content="运行已中断，请检查高级诊断。核对不会自动重复执行；停止请求也可能需要先处理运行恢复状态。" />}
      {message.error_code && <Text type="secondary">错误代码：{message.error_code}</Text>}
      <Space wrap>{["unknown", "recovery_required"].includes(message.state) && <Button disabled={busy} onClick={() => void action(message.id, "retry")}>核对原请求</Button>}
      {TRIAL_ACTIVE.includes(message.state) && message.state !== "abort_requested" && <Button disabled={busy} onClick={() => void action(message.id, "abort")}>停止此回复</Button>}</Space>
    </div>)}</div>
    {error && <Alert type="warning" content={error} />}
    {uncertain && <Button disabled={busy || !query.data?.available} onClick={() => void send(true)}>用原请求编号核对并重试</Button>}
    <Input.TextArea aria-label="应用试聊消息" value={text} onChange={setText} disabled={busy || !!uncertain} autoSize={{ minRows: 3, maxRows: 8 }} placeholder="发送一条测试消息，不要填写凭据" />
    <Button type="primary" loading={busy} disabled={!query.data?.available || active || !!uncertain || !text.trim()} onClick={() => void send()}>发送试聊消息</Button>
    <Text type="secondary">每个应用使用服务器隔离的试聊会话。结果未知时先恢复原请求；停止须以服务器状态为准。</Text>
  </Space></Card>;
}