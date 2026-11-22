import ProjectTaskRecord from "./ProjectTaskRecord";
import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Card, Empty, Input, Space, Spin, Tag, Typography } from "@arco-design/web-react";
import { applicationsApi, applicationBuildUrl } from "./api";
import { managementStateLabel } from "./navigation";
import { acknowledgedDevelopment, canonicalDevelopmentText, DESKTOP_REQUIREMENT_LIMIT, desktopBusy, developmentTextLength, readPendingDevelopment, readyDesktopBuild, rejectedDevelopmentRequest, savePendingDevelopment, type PendingDevelopment } from "./desktopDevelopment";
const { Text, Paragraph } = Typography;
const errorText = (error: unknown) => error instanceof Error ? error.message : String(error);

export default function DesktopDevelopment({ id, onDirty, readOnly = false, versionsOnly = false }: { id: string; onDirty?: (dirty: boolean) => void; readOnly?: boolean; versionsOnly?: boolean }) {
  const messages = useQuery({ queryKey: ["application-management", id], queryFn: ({ signal }) => applicationsApi.messages(id, signal), refetchInterval: 3000 });
  const client = useQueryClient();
  const [text, setText] = useState("");
  const [pending, setPending] = useState<PendingDevelopment>();
  const [restored, setRestored] = useState(false);
  const [storageError, setStorageError] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const gate = useRef(false);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    try { setPending(readPendingDevelopment(sessionStorage, id)); setRestored(true); }
    catch (error) { setStorageError(errorText(error)); }
    return () => { alive.current = false; };
  }, [id]);
  useEffect(() => { onDirty?.(!!text || busy); }, [text, busy, onDirty]);
  useEffect(() => {
    if (!pending || !messages.data || !acknowledgedDevelopment(pending, messages.data)) return;
    try {
      savePendingDevelopment(sessionStorage, id);
      setPending(undefined);
      setText(current => canonicalDevelopmentText(current.trim()) === canonicalDevelopmentText(pending.text) ? "" : current);
      setError("");
      void client.invalidateQueries({ queryKey: ["application", id] });
      void client.invalidateQueries({ queryKey: ["applications"] });
    } catch (error) { setStorageError(errorText(error)); }
  }, [pending, messages.data, id, client]);
  const active = desktopBusy(messages.data || []);
  const blocked = !restored || !!storageError || messages.isPending || messages.isError;
  const draftText = canonicalDevelopmentText(text.trim());
  const draftLength = developmentTextLength(draftText);
  const tooLong = draftLength > DESKTOP_REQUIREMENT_LIMIT;
  async function send() {
    if (gate.current || blocked || readOnly || (!pending && (active || !draftText || tooLong))) return;
    gate.current = true; setBusy(true); setError("");
    let request: PendingDevelopment | undefined;
    try {
      request = pending || { id: crypto.randomUUID().replace(/-/g, ""), text: draftText };
      savePendingDevelopment(sessionStorage, id, request);
      setPending(request);
      await applicationsApi.submit(id, request.id, request.text);
      // Clear only after the persisted record is visible. Refresh/retry reuses this identity.
      if (alive.current) { setText(""); await messages.refetch(); }
    } catch (error) {
      if (alive.current) {
        const rejected = request && rejectedDevelopmentRequest(error, request) ? request : undefined;
        if (rejected) {
          try { savePendingDevelopment(sessionStorage, id); setPending(undefined); setText(rejected.text); }
          catch (problem) { setStorageError(errorText(problem)); }
        }
        setError(rejected ? "需求未被接收，内容已保留；请修改后再发送。" + errorText(error) : errorText(error));
      }
    }
    finally { gate.current = false; if (alive.current) setBusy(false); }
  }
  async function retry(message: string) {
    if (gate.current || blocked || active || pending || readOnly) return;
    if (!window.confirm("继续这个应用的原开发请求？系统会重新核对当前版本和验收条件，不会创建另一条需求。")) return;
    gate.current = true; setBusy(true); setError("");
    try { await applicationsApi.retryDevelopment(id, message); if (alive.current) await messages.refetch(); }
    catch (error) { if (alive.current) setError(errorText(error)); }
    finally { gate.current = false; if (alive.current) setBusy(false); }
  }
  const shown = (messages.data ?? []).filter(message => !versionsOnly || readyDesktopBuild(message) || message.state === "packaging");
  return <Card title={versionsOnly ? "版本与交付" : readOnly ? "开发与验收记录" : "开发对话"}><Space direction="vertical" size={16} style={{ width: "100%" }}>
    <Alert type="info" content="在这里说明要做什么、给谁用，以及怎样算完成。先讨论需求，确认后由开发任务实现、独立验收并生成可用产物。这里不是应用的业务聊天；下载构建不代表已经安装或运行。" />
    {!readOnly && <Space wrap><Button href="#models">检查模型与开发账号</Button><Text type="secondary">开发、测评和应用运行分别使用各自的模型配置。</Text></Space>}
    {storageError && <Alert type="error" content={storageError} />}
    {messages.isError && <Alert type="error" content={errorText(messages.error)} action={<Button onClick={() => void messages.refetch()}>重新读取记录</Button>} />}
    <div className="application-conversation">{messages.isPending ? <Spin /> : !shown.length ? <Empty description={versionsOnly ? "暂无已验收的构建。" : "还没有开发记录。先描述应用需求。"} /> : shown.map(message => <div className="application-message" key={message.id}>
      <Text bold>你的需求</Text><Paragraph className="application-prose">{message.text}</Paragraph>
      <Tag>{message.read_only ? "历史状态 · " : ""}{managementStateLabel(message.state)}</Tag>
      <Paragraph className="application-prose">{message.reply || "请求已保存，等待处理。"}</Paragraph>
      {message.recovery_message && <Alert type={message.read_only ? "warning" : "info"} content={message.recovery_message} />}
      {message.read_only && <Text type="secondary">历史记录仅供查看，不会自动继续执行或重新打包；已保存的验收构建仍需通过校验后下载。</Text>}
      {message.task && <><Paragraph type="secondary">开发任务：{message.task}</Paragraph><ProjectTaskRecord id={id} task={message.task} active={["starting","developing","verifying","packaging"].includes(message.state)} /></>}
      {readyDesktopBuild(message) && <Space direction="vertical">
        <Button href={applicationBuildUrl(id, message.id)}>下载此历史构建</Button>
        <Text type="secondary">{message.launched ? "曾记录到此构建运行；当前运行状态仍需另外核对。" : "尚未记录此构建在接收方的运行验证。"}</Text>
      </Space>}
      {!readOnly && message.state === "failed" && (message.retryable && !message.read_only ? <Button disabled={busy || active || !!pending || blocked} onClick={() => void retry(message.id)}>重试原开发请求</Button> : <Text type="secondary">此历史任务不能继续，请保留证据并提出新的需求。</Text>)}
    </div>)}</div>
    {!readOnly && <>
      {active && <Alert type={messages.data?.some(message => message.blocks_development) ? "warning" : "info"} content={messages.data?.some(message => message.blocks_development)
        ? "此应用有尚无法核对的未完成旧任务。请先处理原任务和证据，再继续开发；不会自动重建任务或覆盖历史，其他应用不受影响。"
        : "这个应用已有开发请求正在处理。可以离开或刷新页面，回来查看进度；不要重复创建任务。"} />}
      {pending && <Alert type="warning" title="正在核对原请求" content={<><Paragraph className="application-prose">{pending.text}</Paragraph><Text>请求可能已经保存。先刷新记录；若仍未出现，可用同一请求编号重试，不会另建需求。</Text></>} action={<Button disabled={busy} onClick={() => void messages.refetch()}>刷新记录</Button>} />}
      <Input.TextArea aria-label="桌面应用开发需求" value={text} disabled={busy || !!pending || active || blocked} onChange={setText} autoSize={{ minRows: 4, maxRows: 10 }} placeholder="例如：做一个桌面伙伴，支持文字聊天和 Live2D 表情。先帮我梳理可运行的最小版本和验收方式。不要在这里填写密钥。" />
      {tooLong && <Alert type="warning" content={`需求最多 ${DESKTOP_REQUIREMENT_LIMIT} 个字符，当前 ${draftLength} 个。内容已保留，请精简后发送。`} />}
      {error && <Alert type="error" content={error} />}
      <Button type="primary" loading={busy} disabled={blocked || (!pending && (active || !draftText || tooLong))} onClick={() => void send()}>{pending ? "重试同一请求" : "发送开发需求"}</Button>
    </>}
  </Space></Card>;
}
