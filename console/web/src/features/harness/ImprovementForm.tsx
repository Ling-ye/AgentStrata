import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Alert, Button, Card, Checkbox, Input, InputNumber, Select, Space, Typography } from "@arco-design/web-react";
import ModelProfileSelect from "../llm/ModelProfileSelect";
import { llmApi, profileOptions } from "../llm/api";
import { improvementApi, type StartImprovement } from "./improvements";
import type { RepairTask } from "./api";

const { Text } = Typography;
const grid = { display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 240px), 1fr))", gap: 16 };

export function ImprovementForm({ created }: { created: (task: RepairTask) => void }) {
  const [type, setType] = useState<"integrate" | "improve">("improve");
  const [runtime, setRuntime] = useState("");
  const [bot, setBot] = useState("");
  const [goal, setGoal] = useState("");
  const [paths, setPaths] = useState("");
  const [suite, setSuite] = useState("agentstrata-application-bridges-v1");
  const [cases, setCases] = useState<string[]>([]);
  const [profile, setProfile] = useState("");
  const [attempts, setAttempts] = useState(3);
  const [hours, setHours] = useState(1);
  const [external, setExternal] = useState(false);
  const [source, setSource] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const submitted = useRef({ body: "", requestId: "" });
  const alive = useRef(true);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  const applications = useQuery({ queryKey: ["harness-applications"], queryFn: ({ signal }) => improvementApi.applications(signal), retry: false });
  const models = useQuery({ queryKey: ["llm-config"], queryFn: ({ signal }) => llmApi.config(signal), retry: false });
  const preview = useQuery({ queryKey: ["harness-application-preview", bot, suite], queryFn: ({ signal }) => improvementApi.preview(bot, suite, signal), enabled: !!bot && !!suite, retry: false });
  const choices = applications.data?.applications.filter(app => !runtime || app.runtime_id === runtime) ?? [];
  const selected = choices.find(app => app.bot_id === bot);
  const worker = profile || models.data?.bindings.harness || "";
  const availableWorker = profileOptions(models.data, true).some(item => item.value === worker);
  const ready = selected && preview.data && !preview.isFetching && !preview.isError && availableWorker && !models.isError;
  async function start() {
    if (!ready || busy || !goal.trim() || !paths.trim()) return;
    const body = { task_type: type, bot_id: bot, goal: goal.trim(), allowed_paths: paths.split("\n").map(value => value.trim()).filter(Boolean),
      goal_case_ids: cases, suite_id: suite, source_envelope: external ? source : {},
      profile: worker, max_attempts: attempts, timeout_seconds: Math.round(hours * 3600) };
    const identity = JSON.stringify(body);
    if (submitted.current.body !== identity) submitted.current = { body: identity, requestId: crypto.randomUUID() };
    setBusy(true); setError("");
    try {
      const task = await improvementApi.start({ ...body, request_id: submitted.current.requestId } as StartImprovement);
      if (alive.current) { submitted.current = { body: "", requestId: "" }; created(task); }
    } catch (error) { if (alive.current) setError(String(error)); }
    finally { if (alive.current) setBusy(false); }
  }
  return <Card title="接入能力或提出改进需求"><Space direction="vertical" size={16} style={{ width: "100%" }}>
    <Text type="secondary">选择应用、描述期望行为并明确可修改范围。Harness 生成本地候选；评测与独立验收后，可在详情中准备和启用版本。</Text>
    <div style={grid}>
      <div>需求类型<Select aria-label="需求类型" disabled={busy} value={type} onChange={setType} options={[{ value: "improve", label: "改进已有能力" }, { value: "integrate", label: "接入外部能力" }]} /></div>
      <div>运行时<Select aria-label="按运行时选择应用" disabled={busy} value={runtime} onChange={value => { setRuntime(value); setBot(""); setCases([]); }} options={[{ value: "", label: "全部运行时" }, { value: "langgraph", label: "LangGraph" }, { value: "codex", label: "Codex" }]} /></div>
      <div>目标应用<Select aria-label="需求目标应用" disabled={busy} value={bot || undefined} loading={applications.isPending} placeholder="选择已配置的应用" onChange={value => { setBot(value); setCases([]); }} options={choices.map(app => ({ value: app.bot_id, label: `${app.display_name} · ${app.runtime_id}` }))} /></div>
    </div>
    {applications.isError && <Alert type="error" content={String(applications.error)} action={<Button onClick={() => void applications.refetch()}>重试</Button>} />}
    {applications.data && !choices.length && <Alert type="info" content="没有符合条件的应用。先在实例配置中创建应用并选择 Agent profile。" />}
    {!!applications.data?.unavailable.length && <Alert type="warning" content={applications.data.unavailable.map(item => `${item.application}：${item.message}`).join("；")} />}
    {selected && <Text type="secondary">Agent 配置：{selected.agent_profile}；本次使用实例配置的 {selected.runtime_id}。开发模型独立选择，运行时筛选不会修改应用配置。</Text>}
    <div>需求与验收期望<Input.TextArea aria-label="需求与验收期望" value={goal} onChange={setGoal} disabled={busy} autoSize={{ minRows: 4, maxRows: 12 }} placeholder="描述要新增或改进的能力、成功条件，以及必须保持的行为" /></div>
    <div>允许修改的源码路径<Input.TextArea aria-label="允许修改的源码路径" value={paths} onChange={setPaths} disabled={busy} autoSize={{ minRows: 2, maxRows: 6 }} placeholder="每行一个仓库内的现有目录或文件，例如 src/chatcopilot/agent/tools" /></div>
    <Text type="secondary">授权仅覆盖填写的源码范围。评分规则与宿主验收保持独立；默认保留应用 Suite 的回归保护。</Text>
    {preview.isFetching && <Text>正在核对应用和评测条件…</Text>}
    {preview.isError && <Alert type="error" content={String(preview.error)} action={<Button onClick={() => void preview.refetch()}>重新核对</Button>} />}
    {preview.data && !preview.isError && <Alert type="info" content={`应用执行：${preview.data.runtime_id} / ${preview.data.evidence?.run?.model ?? "见模型配置"}；保护 ${preview.data.case_ids.length} 个评测用例。每组执行 3 次，并进行候选复测与独立确认。`} />}
    <details><summary>评测目标与外部仓库来源</summary><Space direction="vertical" size={12} style={{ width: "100%", marginTop: 12 }}>
      <div>评测 Suite<Input aria-label="需求评测 Suite" disabled={busy} value={suite} onChange={value => { setSuite(value); setCases([]); }} /></div>
      <div>已有目标用例（可选）<Select aria-label="已有目标用例" mode="multiple" disabled={busy || !preview.data} value={cases} onChange={setCases} options={(preview.data?.case_ids ?? []).map(value => ({ value, label: value }))} /></div>
      <Text type="secondary">不选已有目标用例时，由独立 Test 角色提出新增需求检查，基线与候选执行同一定义。</Text>
      <Checkbox checked={external} disabled={busy} onChange={setExternal}>提供获准调查的外部仓库</Checkbox>
      {external && <div style={grid}>{[["resource_name", "接入名称"], ["source_url", "公开仓库 HTTPS 地址"], ["approved_ref", "固定 commit SHA"], ["license_evidence", "源码许可依据"], ["integration_intent", "接入用途与范围"]].map(([key, label]) => <div key={key}>{label}<Input aria-label={label} value={source[key] ?? ""} disabled={busy} onChange={value => setSource({ ...source, [key]: value })} /></div>)}</div>}
    </Space></details>
    <div style={grid}>
      <div>开发模型<ModelProfileSelect purpose="harness" worker value={profile} onChange={setProfile} disabled={busy} /></div>
      <div>最多候选次数<InputNumber aria-label="需求候选次数" min={1} precision={0} value={attempts} onChange={setAttempts} disabled={busy} style={{ width: "100%" }} /></div>
      <div>总时间预算（小时）<InputNumber aria-label="需求时间预算" min={0.01} precision={2} value={hours} onChange={setHours} disabled={busy} style={{ width: "100%" }} /></div>
    </div>
    {models.isError && <Alert type="error" content="开发模型配置读取失败，请在模型配置页核对连接。" />}
    {error && <Alert type="error" content={error} />}
    <Button type="primary" loading={busy} disabled={!ready || !goal.trim() || !paths.trim()} onClick={() => void start()}>创建需求任务</Button>
  </Space></Card>;
}
