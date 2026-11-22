import { lazy, useContext, useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Button,
  Card,
  Empty,
  Input,
  Space,
  Spin,
  Tabs,
  Tag,
  Typography,
} from "@arco-design/web-react";
import PageSection from "../shared/ui/PageSection";
import { NavigationGuardContext } from "../shared/navigationGuard";
import { applicationsApi, openApplicationUrl, type ApplicationView, type Configuration } from "../features/applications/api";
import {
  applicationHref,
  applicationLocation,
  leavesApplication,
  stateLabel,
  managementStateLabel,
  typeLabel,
  type ApplicationTab,
} from "../features/applications/navigation";
import "../styles/applications.css";
import ApplicationRecords from "../features/applications/ApplicationRecords";
import ApplicationSettings from "../features/applications/ApplicationSettings";
import ImprovementReadiness from "../features/applications/ImprovementReadiness";
import QQDevelopment from "../features/applications/QQDevelopment";
import DesktopDevelopment from "../features/applications/DesktopDevelopment";
import ApplicationInstallers from "../features/applications/ApplicationInstallers";
import TrialChat from "../features/applications/TrialChat";
import {
  acknowledgedDevelopment,
  canonicalDevelopmentText,
  DESKTOP_REQUIREMENT_LIMIT,
  developmentTextLength,
  rejectedDevelopmentRequest,
} from "../features/applications/desktopDevelopment";
import { usePendingApplicationRequest } from "../features/applications/usePendingApplicationRequest";
import ApplicationDirectory, { AppIcon } from "../features/applications/ApplicationDirectory";
import FirstPlanPanel from "../features/applications/FirstPlanPanel";
import { StudioShell, type StudioGroup } from "../features/applications/StudioSidebar";
import { ApplicationQualityScope } from "../features/applications/QualityScope";
import ProjectEvaluation from "../features/applications/ProjectEvaluation";
import ProjectRepair from "../features/applications/ProjectRepair";
import ApplicationVersions from "../features/applications/ApplicationVersions";
import { ImprovementForm } from "../features/harness/ImprovementForm";
const EvalsPage = lazy(() => import("./EvalsPage"));
const HarnessPage = lazy(() => import("./HarnessPage"));
const BotsPage = lazy(() => import("./BotsPage"));
const SchedulesPage = lazy(() => import("./SchedulesPage"));
const ServicesPage = lazy(() => import("./ServicesPage"));
const { Text, Paragraph } = Typography;
const errorText = (value: unknown) => (value instanceof Error ? value.message : String(value));
function useMounted() {
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  return alive;
}
export default function ApplicationsPage({
  hash,
  create = false,
  connect = false,
}: {
  hash: string;
  create?: boolean;
  connect?: boolean;
}) {
  const { id, tab } = applicationLocation(hash);
  return id ? (
    <ApplicationWorkspace key={id} id={id} tab={tab} hash={hash} />
  ) : (
    <ApplicationDirectory create={create} connect={connect} />
  );
}
const menuGroups: [string, [ApplicationTab, string][]][] = [
  [
    "应用",
    [
      ["overview", "基本信息"],
      ["use", "下载与部署"],
    ],
  ],
  [
    "运行监控",
    [
      ["records", "任务流"],
      ["runtime", "运行状态"],
      ["schedules", "定时任务"],
    ],
  ],
  [
    "质量与改进",
    [
      ["evaluations", "测评"],
      ["repairs", "Harness 修复"],
    ],
  ],
  [
    "开发与交付",
    [
      ["improve", "应用改进"],
      ["versions", "版本与交付"],
    ],
  ],
  [
    "应用配置",
    [
      ["configuration", "模型与行为"],
      ["channels", "渠道与工具"],
    ],
  ],
];
function ApplicationWorkspace({ id, tab, hash }: { id: string; tab: ApplicationTab; hash: string }) {
  const [configurationDirty, setConfigurationDirty] = useState(false);
  const [channelDirty, setChannelDirty] = useState(false);
  const [planDirty, setPlanDirty] = useState(false);
  const [managementDirty, setManagementDirty] = useState(false);
  const [trialDirty, setTrialDirty] = useState(false);
  const [developmentDirty, setDevelopmentDirty] = useState(false);
  const register = useContext(NavigationGuardContext);
  const dirty = configurationDirty || channelDirty || managementDirty || trialDirty || developmentDirty || planDirty;
  useEffect(
    () =>
      register({
        shouldBlock: next => dirty && leavesApplication(next, id),
        confirm: async () =>
          window.confirm("当前应用有未保存的配置或消息草稿，确定离开并丢弃吗？已发送的任务不会因此停止。"),
      }),
    [register, dirty, id],
  );
  useEffect(() => {
    const before = (event: BeforeUnloadEvent) => {
      if (dirty) {
        event.preventDefault();
        event.returnValue = "";
      }
    };
    window.addEventListener("beforeunload", before);
    return () => window.removeEventListener("beforeunload", before);
  }, [dirty]);
  const app = useQuery({
    queryKey: ["application", id],
    queryFn: ({ signal }) => applicationsApi.get(id, signal),
    refetchInterval: 5000,
  });
  const [busy, setBusy] = useState("");
  const [result, setResult] = useState("");
  const [error, setError] = useState("");
  const alive = useMounted();
  const client = useQueryClient();
  async function action(verb: "check" | "start" | "stop") {
    if (busy) return;
    if (verb === "start" && !window.confirm("启动后应用将按现有配置处理真实输入。确认启动这个应用？")) return;
    setBusy(verb);
    setError("");
    setResult("");
    try {
      const value = await applicationsApi.action(id, verb);
      if (alive.current)
        setResult(
          value.checks?.map(check => (check.ok ? "通过：" : "待处理：") + check.message).join("；") ||
            value.message ||
            "操作已完成，状态以最新检查为准。",
        );
      void client.invalidateQueries({ queryKey: ["application", id] });
      void client.invalidateQueries({ queryKey: ["applications"] });
    } catch (e) {
      if (alive.current) setError(errorText(e));
    } finally {
      if (alive.current) setBusy("");
    }
  }
  if (app.isPending)
    return (
      <PageSection title="加载应用">
        <Spin />
      </PageSection>
    );
  if (app.isError || !app.data)
    return (
      <PageSection title="应用不可用" extra={<Button href="#applications">返回应用列表</Button>}>
        <Alert
          type="error"
          content={errorText(app.error)}
          action={<Button onClick={() => void app.refetch()}>重试</Button>}
        />
      </PageSection>
    );
  const value = app.data;
  const repo = value.source === "repository",
    studio = value.source === "studio";
  const managedProject = studio && ["desktop", "web", "source", "companion"].includes(value.type);
  const qq = studio && value.type === "qq_official";
  const instance = value.instance_id || "";
  const params = new URLSearchParams(hash.split("?")[1]);
  const task = params.get("task") || "";
  const groups: StudioGroup[] = menuGroups.map(([title, items]) => ({
    title,
    items: items.map(([key, label]) => ({ label, href: applicationHref(id, key) })),
  }));
  const planPending =
    value.first_plan &&
    ["planning", "plan_failed", "plan_review", "initializing", "initialization_failed"].includes(
      value.plan_state || "",
    );
  const sourceHelp = (
    <Space direction="vertical" style={{ width: "100%" }}>
      <ImprovementReadiness id={id} />
      {qq ? (
        <Button href={applicationHref(id, "improve")}>准备当前 QQ 应用源码</Button>
      ) : managedProject ? (
        <Button href={applicationHref(id, "improve")}>查看本应用开发基线与验收</Button>
      ) : (
        <Button href="#connect">连接运行端或导入可验证的桌宠源码</Button>
      )}
    </Space>
  );
  return (
    <>
      <header className="studio-appbar">
        <a href="#applications">← 我的应用</a>
        <AppIcon app={value} />
        <div>
          <strong>{value.name}</strong>
          <small>{typeLabel(value.type)}</small>
        </div>
        <Tag>
          {value.plan_state ? managementStateLabel(value.plan_state) : stateLabel(value.state.process)}
        </Tag>
        {value.source === "release" && <Button type="primary" href={openApplicationUrl(id)} target="_blank" rel="noopener noreferrer">打开应用</Button>}
        {planPending && tab !== "improve" && (
          <Button size="small" href={applicationHref(id, "improve")}>
            查看首版方案
          </Button>
        )}
      </header>
      {value.source === "release" ? (
        <main className="studio-directory"><ApplicationInstallers key={id} application={value} /></main>
      ) : <StudioShell current={applicationHref(id, tab)} groups={groups}>
        <Tabs renderTabHeader={() => <></>} activeTab={tab} destroyOnHide={false} lazyload>
          <Tabs.TabPane key="overview" title="基本信息">
            <Card title="应用基本信息">
              <dl className="studio-info">
                <dt>应用名称</dt>
                <dd>{value.name}</dd>
                <dt>应用标识</dt>
                <dd>{id}</dd>
                <dt>应用形态</dt>
                <dd>{typeLabel(value.type)}</dd>
                <dt>运行进程</dt>
                <dd>{stateLabel(value.state.process)}</dd>
                <dt>渠道</dt>
                <dd>{stateLabel(value.state.channel)}</dd>
                <dt>真实收发</dt>
                <dd>{stateLabel(value.state.end_to_end)}</dd>
              </dl>
            </Card>
          </Tabs.TabPane>
          <Tabs.TabPane key="use" title="下载与部署">
            <ApplicationInstallers key={id} application={value} />
            {repo && instance && value.capabilities.local_deploy && tab === "use" &&
              <BotsPage key={id} fixed={{ id: instance, tab: "deployment" }} loadError={null} />}
            {qq && <Card title="本机部署"><Space wrap>
              <Button href={applicationHref(id, "channels")}>配置官方 QQ 连接</Button>
              <Button href={applicationHref(id, "runtime")}>运行状态与启停</Button>
            </Space></Card>}
          </Tabs.TabPane>
          <Tabs.TabPane key="records" title="任务流">
            {value.capabilities.observation ? (
              <ApplicationRecords application={value} visible={tab === "records"} />
            ) : (
              <Card title="连接运行端">
                <p>当前应用还没有接入业务任务观测。</p>
                <Button href={applicationHref(id, "runtime")}>查看运行状态</Button>
                {qq && <Button href={applicationHref(id, "runtime")}>本机试聊与记录</Button>}
                {value.source === "native" && <Button href="#connect">连接运行端</Button>}
              </Card>
            )}
          </Tabs.TabPane>
          <Tabs.TabPane key="runtime" title="运行状态">
            <Card title="运行状态">
              <Space wrap>
                <Tag>{stateLabel(value.state.process)}</Tag>
                {value.capabilities.control && (
                  <>
                    {qq && (
                      <Button disabled={!!busy} onClick={() => void action("check")}>
                        检查
                      </Button>
                    )}
                    <Button loading={busy === "start"} disabled={!!busy} onClick={() => void action("start")}>
                      启动
                    </Button>
                    <Button loading={busy === "stop"} disabled={!!busy} onClick={() => void action("stop")}>
                      停止
                    </Button>
                  </>
                )}
                {value.source === "native" && <Button href="#connect">连接运行端</Button>}
              </Space>
              {error && <Alert type="error" content={error} />}{" "}
              {result && <Alert type="info" content={result} />}
            </Card>
            {qq && <TrialChat id={id} onDirty={setTrialDirty} />}
            {repo && instance && tab === "runtime" && (
              <>
                <BotsPage key={id} fixed={{ id: instance, tab: "runtime" }} loadError={null} />
                <ServicesPage scope={instance} />
              </>
            )}
          </Tabs.TabPane>
          <Tabs.TabPane key="schedules" title="定时任务">
            {repo && instance ? (
              <SchedulesPage instanceId={instance} />
            ) : (
              <Empty description="此来源未接入应用定时任务。" />
            )}
          </Tabs.TabPane>
          <Tabs.TabPane key="evaluations" title="测评">
            {tab === "evaluations" &&
              (repo && instance ? (
                <ApplicationQualityScope key={id} id={id}>
                  <EvalsPage initialApplication={instance} />
                </ApplicationQualityScope>
              ) : (
                <ProjectEvaluation application={value} help={sourceHelp} />
              ))}
          </Tabs.TabPane>
          <Tabs.TabPane key="repairs" title="Harness 修复">
            {tab === "repairs" &&
              (repo && instance ? (
                <ApplicationQualityScope key={id} id={id}>
                  <HarnessPage key={params.get("sourceId") || id} initialApplication={instance} />
                </ApplicationQualityScope>
              ) : (
                <>
                  {params.get("sourceId") && (
                    <ProjectRepair
                      key={params.get("sourceId")}
                      id={id}
                      kind={params.get("sourceKind") || "robot_task"}
                      sourceId={params.get("sourceId")!}
                      evaluation={params.get("evaluation") || ""}
                    />
                  )}
                  {sourceHelp}
                  {qq && <QQDevelopment id={id} mode="records" />}
                  {managedProject && <DesktopDevelopment id={id} readOnly />}
                </>
              ))}
          </Tabs.TabPane>
          <Tabs.TabPane key="improve" title="应用改进">
            <Space direction="vertical" style={{ width: "100%" }}>
              {value.first_plan && <FirstPlanPanel id={id} onDirty={setPlanDirty} />}
              {!planPending &&
                (repo && instance ? (
                  <ApplicationQualityScope key={id} id={id}>
                    <ImprovementForm
                      initialApplication={instance}
                      created={t => {
                        if (applicationLocation(window.location.hash).id === id)
                          window.location.hash = applicationHref(id, "repairs", { task: t.task_id });
                      }}
                    />
                  </ApplicationQualityScope>
                ) : managedProject ? (
                  <DesktopDevelopment id={id} onDirty={setManagementDirty} />
                ) : qq ? (
                  <>
                    <ManagementConversation
                      id={id}
                      application={value}
                      onDirty={setManagementDirty}
                      configurationDirty={configurationDirty || channelDirty}
                    />
                    <QQDevelopment id={id} onDirty={setDevelopmentDirty} />
                  </>
                ) : (
                  sourceHelp
                ))}
            </Space>
          </Tabs.TabPane>
          <Tabs.TabPane key="versions" title="版本与交付">
            {tab === "versions" &&
              (repo ? (
                <ApplicationQualityScope key={id} id={id}>
                  <ApplicationVersions id={id} taskId={task} />
                </ApplicationQualityScope>
              ) : managedProject ? (
                <DesktopDevelopment id={id} readOnly versionsOnly />
              ) : qq ? (
                <QQDevelopment id={id} mode="versions" />
              ) : (
                sourceHelp
              ))}
          </Tabs.TabPane>
          <Tabs.TabPane key="configuration" title="模型与行为">
            {value.capabilities.settings ? (
              <ApplicationSettings id={id} onDirty={setConfigurationDirty} />
            ) : qq ? (
              <Button href={applicationHref(id, "channels")}>配置 QQ 官方连接</Button>
            ) : (
              <Card title="模型与行为">
                <Text>模型与行为由此应用的 Agent 配置管理。</Text>
                <Button href={applicationHref(id, "improve")}>进入应用改进</Button>
              </Card>
            )}
          </Tabs.TabPane>
          <Tabs.TabPane key="channels" title="渠道与工具">
            {(repo || value.source === "plugin") && instance ? (
              <BotsPage
                key={id}
                fixed={{ id: instance, tab: "configuration" }}
                visible={tab === "channels"}
                loadError={null}
              />
            ) : qq ? (
              <ApplicationConfiguration id={id} onDirty={setChannelDirty} />
            ) : (
              <Card title="渠道与工具">
                <Text>由该应用宿主管理渠道和工具。</Text>
              </Card>
            )}
          </Tabs.TabPane>
        </Tabs>
      </StudioShell>}
    </>
  );
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
  const { pending, restored, storageError, remember, acknowledge } = usePendingApplicationRequest(id, "qq-configuration");
  const gate = useRef(false);
  const alive = useMounted();
  const client = useQueryClient();
  const active = messages.data?.some(message => ["console-thinking", "thinking", "starting", "developing", "verifying", "packaging"].includes(message.state));
  const blocked = !restored || !!storageError || messages.isPending || messages.isError;
  const draft = canonicalDevelopmentText(text.trim());
  const tooLong = developmentTextLength(draft) > DESKTOP_REQUIREMENT_LIMIT;
  useEffect(() => {
    if (!pending || !messages.data || !acknowledgedDevelopment(pending, messages.data)) return;
    try {
      if (acknowledge(pending)) {
        setText(current => canonicalDevelopmentText(current.trim()) === canonicalDevelopmentText(pending.text) ? "" : current);
        setError("");
      }
    } catch { /* The hook preserves the request and reports the storage failure. */ }
  }, [pending, messages.data, acknowledge]);
  async function send() {
    if (gate.current || blocked || (!pending && (active || !draft || tooLong))) return;
    const submitted = pending || { id: crypto.randomUUID().replace(/-/g, ""), text: draft };
    gate.current = true;
    setBusy(true); setError("");
    try { remember(submitted); await applicationsApi.submit(id, submitted.id, submitted.text); if (alive.current) { setText(""); await messages.refetch(); } }
    catch (e) {
      if (alive.current) {
        if (rejectedDevelopmentRequest(e, submitted)) {
          try { if (acknowledge(submitted)) setText(submitted.text); } catch { /* Storage failure remains visible. */ }
        }
        setError(errorText(e));
      }
    }
    finally { gate.current = false; if (alive.current) setBusy(false); }
  }
  async function apply(message: string) {
    if (gate.current || blocked || pending || active || configurationDirty || !window.confirm("确认将这份提案应用到当前应用配置？此操作不会发送 QQ 消息。")) return;
    gate.current = true;
    setBusy(true); setError("");
    try { await applicationsApi.apply(id, message); if (alive.current) setApplied(message); void client.invalidateQueries({ queryKey: ["application-management", id] }); void client.invalidateQueries({ queryKey: ["application-configuration", id] }); void client.invalidateQueries({ queryKey: ["application", id] }); }
    catch (e) { if (alive.current) setError(errorText(e)); }
    finally { gate.current = false; if (alive.current) setBusy(false); }
  }
  const latest = messages.data?.filter(message => message.proposal).slice(-1)[0]?.id;
  return <Card title="修改这个应用"><Space direction="vertical" size={16} style={{ width: "100%" }}>
    <Alert type="info" content={application.type === "qq_official" ? "这里与管理助手讨论应用配置，不是与 QQ 机器人聊天。提案需要确认；秘密请在配置页填写。提示词改进须在上方独立开发流程中明确启动。" : "这是应用开发与管理对话，可能创建需要开发账号的 Harness 任务，不是业务聊天。请明确改动范围与验收要求。"} />
    {applied && <Alert type="success" content="提案已应用到当前应用配置。" />}
    {configurationDirty && <Alert type="warning" content="配置页有未保存更改，请先保存或放弃，再应用对话提案。" />}
    {storageError && <Alert type="error" content={storageError} />}
    {messages.isError && <Alert type="error" content={errorText(messages.error)} action={<Button onClick={() => void messages.refetch()}>重新读取记录</Button>} />}
    <div className="application-conversation">{messages.isPending ? <Spin /> : !messages.data?.length ? <Empty description="描述你想创建或改进的应用" /> : messages.data.map(message => <div className="application-message" key={message.id}>
      <Text bold>你的需求</Text><Paragraph className="application-prose">{message.text}</Paragraph>
      <Tag>{managementStateLabel(message.state)}</Tag><Paragraph className="application-prose">{message.reply || "正在处理…"}</Paragraph>
      {message.proposal && <Card title="配置提案"><Text bold>{message.proposal.name}</Text><Paragraph className="application-prose">{message.proposal.instructions}</Paragraph><Button disabled={busy || blocked || !!pending || active || configurationDirty || message.id !== latest || message.state !== "configure" || message.id === applied} onClick={() => void apply(message.id)}>{message.state === "applied" || message.id === applied ? "提案已应用" : "确认应用提案"}</Button></Card>}
    </div>)}</div>
    {pending && <Alert type="warning" title="正在核对原配置需求" content={<><Paragraph className="application-prose">{pending.text}</Paragraph><Text>刷新后保留同一请求编号，核对前不另建需求。</Text></>} action={<Button disabled={busy} onClick={() => void messages.refetch()}>刷新核对状态</Button>} />}
    {active && <Alert type="info" content="此应用已有配置或开发请求正在处理，可以刷新或离开后继续查看。" />}
    <Input.TextArea aria-label="应用改进需求" value={text} onChange={setText} disabled={busy || blocked || !!pending || active} autoSize={{ minRows: 3, maxRows: 8 }} placeholder="描述希望改变的行为，不要填写密钥" />
    {tooLong && <Alert type="warning" content={`需求最多 ${DESKTOP_REQUIREMENT_LIMIT} 个字符，内容已保留，请精简后发送。`} />}
    {error && <Alert type="error" content={error} />}
    <Button type="primary" loading={busy} disabled={blocked || (!pending && (active || !draft || tooLong))} onClick={() => void send()}>{pending ? "重试同一配置需求" : "发送改进需求"}</Button>
  </Space></Card>;
}
