import { useContext, useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Card, Empty, Input, Select, Space, Spin, Tabs, Tag, Typography } from "@arco-design/web-react";
import PageSection from "../shared/ui/PageSection";
import { NavigationGuardContext } from "../shared/navigationGuard";
import { applicationsApi, applicationBuildUrl, type ApplicationView, type Configuration } from "../features/applications/api";
import { applicationHref, applicationLocation, leavesApplication, localLink, stateLabel, managementStateLabel, typeLabel, type ApplicationTab } from "../features/applications/navigation";
import "../styles/applications.css";
import ImprovementReadiness from "../features/applications/ImprovementReadiness";
import ApplicationEvidence from "../features/applications/ApplicationEvidence";
import QQDevelopment from "../features/applications/QQDevelopment";
import DesktopLaunch from "../features/applications/DesktopLaunch";
import TrialChat from "../features/applications/TrialChat";
const { Text, Paragraph } = Typography;
const errorText = (value: unknown) => value instanceof Error ? value.message : String(value);
function useMounted() { const alive = useRef(true); useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []); return alive; }
function ApplicationLink({ href, children }: { href?: string; children: React.ReactNode }) {
  const safe = localLink(href);
  return safe ? <Button href={safe}>{children}</Button> : null;
}

export default function ApplicationsPage({ hash, create = false }: { hash: string; create?: boolean }) {
  const location = applicationLocation(hash);
  const list = useQuery({ queryKey: ["applications"], queryFn: ({ signal }) => applicationsApi.list(signal), refetchInterval: 10000 });
  if (create) return <CreateApplication />;
  if (location.id) return <ApplicationWorkspace key={location.id} id={location.id} tab={location.tab} />;
  return <PageSection title="我的应用" description="打开一个应用，在同一工作区使用、配置和改进。应用实例与它使用的组件分开管理。" extra={<><Button onClick={() => void list.refetch()}>刷新</Button><Button type="primary" href="#create">创建应用</Button></>}>
    {list.isPending ? <Spin /> : list.isError ? <Alert type="error" content={errorText(list.error)} /> :
      !list.data?.applications.length ? <Empty description="还没有应用。先创建一个 QQ 助手，或接入已有应用。" /> :
      <div className="applications-grid">{list.data.applications.map(app => <Card key={app.id} title={app.name} extra={<Tag>{typeLabel(app.type)}</Tag>}>
        <Space direction="vertical" style={{ width: "100%" }}>
          <Text type="secondary">{stateLabel(app.state.process)} · 渠道：{stateLabel(app.state.channel)}</Text>
          <Text type="secondary">端到端：{stateLabel(app.state.end_to_end)}</Text>
          {app.message && <Paragraph>{app.message}</Paragraph>}
          <Button type="primary" href={applicationHref(app.id)}>进入应用</Button>
        </Space>
      </Card>)}</div>}
  </PageSection>;
}
function CreateApplication() {
  const [type, setType] = useState("qq_official");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const requestId = useRef(crypto.randomUUID());
  const alive = useMounted();
  const client = useQueryClient();
  async function create() {
    if (busy) return;
    setBusy(true); setError("");
    try {
      const app = await applicationsApi.create(requestId.current, type);
      void client.invalidateQueries({ queryKey: ["applications"] });
      if (alive.current) window.location.hash = applicationHref(app.id, "improve");
    } catch (e) { if (alive.current) setError(errorText(e)); }
    finally { if (alive.current) setBusy(false); }
  }
  return <PageSection title="创建应用" description="先选择应用类型，再通过对话描述用途。连接凭据在专用配置表单中填写。">
    <Card className="application-form"><Space direction="vertical" size={20} style={{ width: "100%" }}>
      <Select aria-label="应用类型" value={type} disabled={busy} onChange={value => { setType(value); requestId.current = crypto.randomUUID(); }} options={[
        { value: "qq_official", label: "QQ 助手 · QQ 官方机器人" }, { value: "desktop", label: "桌面应用 · 请使用原开发入口", disabled: true }, { value: "live2d", label: "桌面伙伴 · 尚未迁入创建流程", disabled: true },
      ]} />
      <Alert type="info" content={type === "qq_official" ? "标准 QQ 应用可使用 API 模型；仍需 QQ 平台机器人凭据和允许的测试范围。本机关闭时应用离线。" :
        type === "live2d" ? "连接现有桌面伙伴并创建管理项目；本操作不是安装或远程启动 Windows 桌宠。开发功能需配置开发账号。" : "创建开发项目，代码改进使用既有 Harness。开发模型和运行模型分别配置。"} />
      <Button href="#models">模型与连接</Button>
      {error && <Alert type="error" content={error} />}
      <Button type="primary" loading={busy} onClick={() => void create()}>创建并进入应用</Button>
    </Space></Card>
  </PageSection>;
}
function ApplicationWorkspace({ id, tab }: { id: string; tab: ApplicationTab }) {
  const [configurationDirty, setConfigurationDirty] = useState(false);
  const [managementDirty, setManagementDirty] = useState(false);
  const [trialDirty, setTrialDirty] = useState(false);
  const [developmentDirty, setDevelopmentDirty] = useState(false);
  const register = useContext(NavigationGuardContext);
  const dirty = configurationDirty || managementDirty || trialDirty || developmentDirty;
  useEffect(() => register({ shouldBlock: next => dirty && leavesApplication(next, id), confirm: async () => window.confirm("当前应用有未保存的配置或消息草稿，确定离开并丢弃吗？已发送的任务不会因此停止。") }), [register, dirty, id]);
  useEffect(() => { const before = (event: BeforeUnloadEvent) => { if (dirty) { event.preventDefault(); event.returnValue = ""; } }; window.addEventListener("beforeunload", before); return () => window.removeEventListener("beforeunload", before); }, [dirty]);
  const app = useQuery({ queryKey: ["application", id], queryFn: ({ signal }) => applicationsApi.get(id, signal), refetchInterval: 5000 });
  const [busy, setBusy] = useState("");
  const [result, setResult] = useState("");
  const [error, setError] = useState("");
  const alive = useMounted();
  const client = useQueryClient();
  async function action(verb: "check" | "start" | "stop") {
    if (busy) return;
    if (verb === "start" && !window.confirm("启动后应用将按现有配置处理真实输入。确认启动这个应用？")) return;
    setBusy(verb); setError(""); setResult("");
    try {
      const value = await applicationsApi.action(id, verb);
      if (alive.current) setResult(value.checks?.map(check => (check.ok ? "通过：" : "待处理：") + check.message).join("；") || value.message || "操作已完成，状态以最新检查为准。");
      void client.invalidateQueries({ queryKey: ["application", id] }); void client.invalidateQueries({ queryKey: ["applications"] });
    } catch (e) { if (alive.current) setError(errorText(e)); }
    finally { if (alive.current) setBusy(""); }
  }
  if (app.isPending) return <PageSection title="加载应用"><Spin /></PageSection>;
  if (app.isError || !app.data) return <PageSection title="应用不可用" extra={<Button href="#applications">返回应用列表</Button>}><Alert type="error" content={errorText(app.error)} action={<Button onClick={() => void app.refetch()}>重试</Button>} /></PageSection>;
  const value = app.data;
  return <PageSection title={value.name} description={<>{typeLabel(value.type)} · {id}</>} extra={<Button href="#applications">所有应用</Button>}>
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      <Space wrap><Tag>{stateLabel(value.state.process)}</Tag><Text>渠道：{stateLabel(value.state.channel)}；真实收发：{stateLabel(value.state.end_to_end)}</Text>
        {value.capabilities.control && <>{value.capabilities.configure && value.source === "studio" && <Button loading={busy === "check"} disabled={!!busy} onClick={() => void action("check")}>检查</Button>}<Button loading={busy === "start"} disabled={!!busy} onClick={() => void action("start")}>启动</Button><Button loading={busy === "stop"} disabled={!!busy} onClick={() => void action("stop")}>停止</Button></>}
      </Space>
      {error && <Alert type="error" content={error} />}{result && <Alert type="info" content={result} />}
      <Tabs activeTab={tab} onChange={next => { window.location.hash = applicationHref(id, next as ApplicationTab); }} destroyOnHide={false}>
        <Tabs.TabPane key="overview" title="概览"><Card><Space direction="vertical" size={16}>
          <Text>{value.message || "请选择使用、配置、记录或改进。进程运行不等于真实消息已完成投递。"}</Text>
          <Text type="secondary">来源：{value.source}。应用名称可以变化，稳定标识用于隔离配置、操作和记录。</Text>
          <Text>配置 → 检查 → 启动 → 在目标平台验证真实收发。</Text>
        </Space></Card></Tabs.TabPane>
        <Tabs.TabPane key="use" title="使用">{value.source === "studio" && ["desktop", "companion"].includes(value.type) ? <DesktopLaunch key={id} id={id} /> : value.capabilities.chat || (value.source === "studio" && value.type === "qq_official") ? <TrialChat id={id} onDirty={setTrialDirty} /> : <Card><Space direction="vertical">
          <Alert type="info" title="应用使用与管理对话相互独立" content="此应用尚未接入安全试聊。这里不会向真实 QQ 发送消息，也不会把改进对话作为机器人聊天。" />
          <Paragraph>{value.type === "qq_official" ? "在配置页连接官方 QQ 机器人，启动后用平台允许的测试账号发送消息，再核对回复与投递。连接成功不能替代这一步。" : ["live2d", "companion", "desktop"].includes(value.type) ? "现有版本尚未记录可信的安装位置和可执行文件校验信息，请从已安装客户端打开。工作台不会猜测路径或直接启动桌面程序。" : "请使用该应用已有的用户入口；工作台管理不会自动开启业务会话。"}</Paragraph>
        </Space></Card>}</Tabs.TabPane>
        <Tabs.TabPane key="configuration" title="配置">{value.capabilities.configure && value.source === "studio" && value.type === "qq_official" ? <ApplicationConfiguration id={id} onDirty={setConfigurationDirty} /> : <Card><Space direction="vertical"><Text>沿用该应用已有配置与权限边界。</Text><ApplicationLink href={value.links.configuration}>打开应用配置</ApplicationLink>{!value.links.configuration && <Text type="secondary">此来源尚无工作台配置入口。</Text>}</Space></Card>}</Tabs.TabPane>
        <Tabs.TabPane key="records" title="记录"><Card className="application-records"><Space direction="vertical" style={{ width: "100%" }}>
          <ApplicationLink href={value.links.records}>查看该应用运行记录</ApplicationLink>
          {value.source === "studio" && value.type === "qq_official" && <ImprovementReadiness id={id} />}
          {value.source === "repository" && <ApplicationEvidence id={id} evalLink={value.links.eval} harnessLink={value.links.harness} />}
          {!value.links.records && <Text type="secondary">此应用的业务运行记录尚未接入统一查看。</Text>}
          <Text>停止应用不会删除历史。业务运行记录与管理、开发对话分开。</Text>
        {value.source === "studio" && value.capabilities.records && !value.capabilities.improve && <ManagementHistory id={id} />}</Space></Card></Tabs.TabPane>
        <Tabs.TabPane key="improve" title="改进"><Space direction="vertical" size={16} style={{ width: "100%" }}>
          {value.source === "studio" && value.type === "qq_official" && <ImprovementReadiness id={id} />}
          {value.source === "studio" && value.type === "qq_official" && <QQDevelopment id={id} onDirty={setDevelopmentDirty} />}
          {value.capabilities.improve && value.project_id ? <ManagementConversation id={id} application={value} onDirty={setManagementDirty} configurationDirty={configurationDirty} /> : <Alert type="info" content="此应用沿用现有改进入口；本页不会自动执行开发或修改配置。" />}
          <Space wrap><ApplicationLink href={value.links.eval}>检查应用表现</ApplicationLink><ApplicationLink href={value.links.harness}>打开该应用的 Harness</ApplicationLink></Space>
        </Space></Tabs.TabPane>
      </Tabs>
    </Space>
  </PageSection>;
}
function ApplicationConfiguration({ id, onDirty }: { id: string; onDirty: (dirty: boolean) => void }) {
  const query = useQuery({ queryKey: ["application-configuration", id], queryFn: ({ signal }) => applicationsApi.configuration(id, signal), retry: false });
  const [draft, setDraft] = useState<Configuration>();
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);
  const client = useQueryClient();
  const alive = useMounted();
  useEffect(() => { if (!dirty && !busy && query.data) setDraft({ name: query.data.name, instructions: query.data.instructions, app_id: query.data.app_id, app_secret: "" }); }, [dirty, busy, query.data]);
  useEffect(() => { onDirty(dirty || busy); }, [dirty, busy, onDirty]);
  function edit(patch: Partial<Configuration>) { setDraft(current => current ? { ...current, ...patch } : current); setDirty(true); setSaved(false); }
  async function save() {
    if (!draft || busy) return;
    const submitted = { ...draft };
    setDraft({ ...draft, app_secret: "" }); setBusy(true); setError("");
    try {
      const saved = await applicationsApi.save(id, submitted);
      client.setQueryData(["application-configuration", id], saved);
      if (alive.current) { setDirty(false); setSaved(true); }
      void client.invalidateQueries({ queryKey: ["application", id] }); void client.invalidateQueries({ queryKey: ["application-configuration", id] }); void client.invalidateQueries({ queryKey: ["applications"] });
    } catch (e) { if (alive.current) setError(errorText(e)); }
    finally { if (alive.current) setBusy(false); }
  }
  return <Card title="QQ 官方连接与应用行为" className="application-form">
    {!draft ? query.isError ? <Alert type="error" content={errorText(query.error)} /> : <Spin /> : <Space direction="vertical" size={16} style={{ width: "100%" }}>
      <Alert type="info" content="密钥只填在专用字段，不进入管理对话或模型。已保存的密钥留空保留；本页不回显密钥。" />
      <label>应用名称<Input aria-label="应用名称" value={draft.name} disabled={busy} onChange={name => edit({ name })} /></label>
      <label>用途与回复要求<Input.TextArea aria-label="用途与回复要求" value={draft.instructions} disabled={busy} onChange={instructions => edit({ instructions })} autoSize={{ minRows: 4, maxRows: 10 }} /></label>
      <label>QQ AppID<Input aria-label="QQ AppID" value={draft.app_id} disabled={busy} onChange={app_id => edit({ app_id })} autoComplete="off" /></label>
      <label>AppSecret{query.data?.secret_configured ? "（已保存，留空保留）" : ""}<Input.Password aria-label="QQ AppSecret" value={draft.app_secret || ""} disabled={busy} onChange={app_secret => edit({ app_secret })} autoComplete="new-password" /></label>
      {error && <Alert type="error" content={error} />}{saved && <Alert type="success" content="配置已保存。请检查并按需启动；真实收发需要平台测试。" />}
      <Space wrap><Button type="primary" loading={busy} disabled={!dirty || !draft.name.trim()} onClick={() => void save()}>保存配置</Button><Button disabled={!dirty || busy} onClick={() => { setDraft(undefined); setDirty(false); }}>放弃修改</Button><Button href="https://bot.q.qq.com/" target="_blank" rel="noopener noreferrer">QQ 官方平台</Button></Space>
    </Space>}
  </Card>;
}
function ManagementConversation({ id, application, onDirty, configurationDirty }: { id: string; application: ApplicationView; onDirty: (dirty: boolean) => void; configurationDirty: boolean }) {
  const messages = useQuery({ queryKey: ["application-management", id], queryFn: ({ signal }) => applicationsApi.messages(id, signal), refetchInterval: 3000 });
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [applied, setApplied] = useState("");
  useEffect(() => { onDirty(!!text || busy); }, [text, busy, onDirty]);
  const request = useRef({ text: "", id: "" });
  const alive = useMounted();
  const client = useQueryClient();
  async function send() {
    if (busy || !text.trim()) return;
    if (request.current.text !== text.trim()) request.current = { text: text.trim(), id: crypto.randomUUID() };
    setBusy(true); setError("");
    try { await applicationsApi.submit(id, request.current.id, request.current.text); if (alive.current) { setText(""); request.current = { text: "", id: "" }; } void messages.refetch(); }
    catch (e) { if (alive.current) setError(errorText(e)); }
    finally { if (alive.current) setBusy(false); }
  }
  async function apply(message: string) {
    if (busy || configurationDirty || !window.confirm("确认将这份提案应用到当前应用配置？此操作不会发送 QQ 消息。")) return;
    setBusy(true); setError("");
    try { await applicationsApi.apply(id, message); if (alive.current) setApplied(message); void client.invalidateQueries({ queryKey: ["application-management", id] }); void client.invalidateQueries({ queryKey: ["application-configuration", id] }); void client.invalidateQueries({ queryKey: ["application", id] }); }
    catch (e) { if (alive.current) setError(errorText(e)); }
    finally { if (alive.current) setBusy(false); }
  }
  const latest = messages.data?.filter(message => message.proposal).slice(-1)[0]?.id;
  return <Card title="修改这个应用"><Space direction="vertical" size={16} style={{ width: "100%" }}>
    <Alert type="info" content={application.type === "qq_official" ? "这里与管理助手讨论应用配置，不是与 QQ 机器人聊天。提案需要确认；秘密请在配置页填写。提示词改进须在上方独立开发流程中明确启动。" : "这是应用开发与管理对话，可能创建需要开发账号的 Harness 任务，不是业务聊天。请明确改动范围与验收要求。"} />
    {applied && <Alert type="success" content="提案已应用到当前应用配置。" />}
    {configurationDirty && <Alert type="warning" content="配置页有未保存更改，请先保存或放弃，再应用对话提案。" />}
    {messages.isError && <Alert type="error" content={errorText(messages.error)} />}
    <div className="application-conversation">{messages.isPending ? <Spin /> : !messages.data?.length ? <Empty description="描述你想创建或改进的应用" /> : messages.data.map(message => <div className="application-message" key={message.id}>
      <Text bold>你的需求</Text><Paragraph className="application-prose">{message.text}</Paragraph>
      <Tag>{managementStateLabel(message.state)}</Tag><Paragraph className="application-prose">{message.reply || "正在处理…"}</Paragraph>
      {message.proposal && <Card title="配置提案"><Text bold>{message.proposal.name}</Text><Paragraph className="application-prose">{message.proposal.instructions}</Paragraph><Button disabled={busy || configurationDirty || message.id !== latest || message.state !== "configure" || message.id === applied} onClick={() => void apply(message.id)}>{message.state === "applied" || message.id === applied ? "提案已应用" : "确认应用提案"}</Button></Card>}
    </div>)}</div>
    <Input.TextArea aria-label="应用改进需求" value={text} onChange={setText} disabled={busy} autoSize={{ minRows: 3, maxRows: 8 }} placeholder="描述希望改变的行为，不要填写密钥" />
    {error && <Alert type="error" content={error} />}
    <Button type="primary" loading={busy} disabled={!text.trim()} onClick={() => void send()}>发送改进需求</Button>
  </Space></Card>;
}

function ManagementHistory({ id }: { id: string }) {
  const messages = useQuery({ queryKey: ["application-management", id], queryFn: ({ signal }) => applicationsApi.messages(id, signal) });
  return <div className="application-conversation"><Text bold>管理与开发历史（只读）</Text><Paragraph>这里不是业务聊天。继续开发请使用原 Studio 入口。已有构建仅提供下载，不会自动安装或启动。</Paragraph>
    {messages.isPending ? <Spin /> : messages.isError ? <Alert type="error" content={errorText(messages.error)} /> : !messages.data?.length ? <Empty description="暂无管理记录" /> : messages.data.map(message => <div className="application-message" key={message.id}><Tag>{managementStateLabel(message.state)}</Tag><Paragraph className="application-prose">{message.text}</Paragraph><Paragraph className="application-prose">{message.reply}</Paragraph>{message.state === "ready" && message.build === message.id && <Button href={applicationBuildUrl(id, message.id)}>下载此构建</Button>}</div>)}
  </div>;
}