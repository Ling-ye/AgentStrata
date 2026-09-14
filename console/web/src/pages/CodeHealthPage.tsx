import { useEffect, useRef, useState } from "react";
import { useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Descriptions, Drawer, Empty, Input, InputNumber, Select, Space, Spin, Table, Tabs, Tag, Typography } from "@arco-design/web-react";
import PageSection from "../shared/ui/PageSection";
import { api } from "../api";
import { repairModels } from "../features/harness/repairModels";
import { ACTIVE, harnessApi, stageLabel } from "../features/harness/api";
import { RepairProgress } from "../features/harness/RepairProgress";
import { healthApi, healthLabels, healthStatus, healthSummary, findingStatus, type Check, type Finding, type HealthTask, type Scope } from "../features/codeHealth/api";
import "../features/codeHealth/style.css";
import HealthLedger from "../features/codeHealth/HealthLedger";

const { Text } = Typography;
const taskFromHash = () => new URLSearchParams(window.location.hash.split("?")[1] ?? "").get("task") ?? "";

export default function CodeHealthPage() {
  const client = useQueryClient();
  const [scope, setScope] = useState<Scope>("all");
  const [model, setModel] = useState("");
  const [effort, setEffort] = useState("medium");
  const [attempts, setAttempts] = useState(3);
  const [hours, setHours] = useState(2);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState("");
  const [taskId, setTaskId] = useState(taskFromHash);
  const [page, setPage] = useState(1);
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("");
  const [rulesOpen, setRulesOpen] = useState(false);
  const submitted = useRef({ body: "", requestId: "" });
  const config = useQuery({ queryKey: ["code-health-config"], queryFn: ({ signal }) => healthApi.config(signal), retry: false });
  const bots = useQuery({ queryKey: ["bots"], queryFn: api.listBots });
  const inspections = useQueries({ queries: (bots.data ?? []).map(bot => ({
    queryKey: ["inspection", bot.instance_id],
    queryFn: ({ signal }: { signal: AbortSignal }) => api.inspection(bot.instance_id, undefined, undefined, signal),
    retry: false, staleTime: 60_000,
  })) });
  const configuredModels = inspections.map(query => repairModels(query.data?.current));
  const defaultModel = config.data?.default_model ?? "";
  const modelOptions = [...new Set([defaultModel, ...configuredModels.flatMap(result => result.options.map(option => option.value))])]
    .filter(Boolean).map(value => ({ value, label: value === defaultModel ? `${value}（治理默认配置）` : value }));
  const modelsLoading = bots.isPending || inspections.some(query => query.isPending);
  const modelsUnavailable = bots.isError || inspections.some((query, index) => query.isError || (!!query.data && !!configuredModels[index].error));
  const history = useQuery({ queryKey: ["code-health-history", page, search, status],
    queryFn: ({ signal }) => healthApi.history(page, search, status, signal), retry: false, refetchInterval: 5000 });
  useEffect(() => {
    const update = () => setTaskId(taskFromHash());
    window.addEventListener("hashchange", update);
    return () => window.removeEventListener("hashchange", update);
  }, []);
  function openTask(id: string) {
    setTaskId(id);
    window.location.hash = id ? `code-health?task=${encodeURIComponent(id)}` : "code-health";
  }
  async function start() {
    const body = { scope, model: model.trim() || config.data?.default_model || "", reasoning_effort: effort,
      max_attempts: attempts, timeout_seconds: Math.round(hours * 3600) };
    const encoded = JSON.stringify(body);
    if (submitted.current.body !== encoded) submitted.current = { body: encoded, requestId: crypto.randomUUID() };
    setStarting(true); setError("");
    try {
      const task = await healthApi.start({ ...body, request_id: submitted.current.requestId });
      submitted.current = { body: "", requestId: "" };
      openTask(task.task_id);
      await client.invalidateQueries({ queryKey: ["code-health-history"] });
    } catch (e) { setError(String(e)); }
    finally { setStarting(false); }
  }
  return <div className="code-health-page">
    <PageSection title="代码治理" description="检查工程规则与代码漂移，生成可审核的清理候选。">
      <Space direction="vertical" size={16} style={{ width: "100%" }}>
        {config.isError && <Alert type="error" content={String(config.error)} action={<Button onClick={() => void config.refetch()}>重试</Button>} />}
        <div className="code-health-form">
          <div className="code-health-field">扫描范围<Select aria-label="扫描范围" value={scope} options={config.data?.scopes ?? []}
            loading={config.isPending} onChange={setScope} disabled={starting || !config.data} /></div>
          <div className="code-health-field">Codex 模型<Select aria-label="Codex 模型" value={model || defaultModel || undefined}
            onChange={setModel} disabled={starting} options={modelOptions} loading={modelsLoading} showSearch allowCreate
            placeholder="选择或输入 Codex 模型" /></div>
        </div>
        <Text type="secondary">模型选项来自治理默认配置和已有的机器人编码配置，也可输入其他 Codex 模型名称。</Text>
        {modelsUnavailable && <Alert type="warning" content="部分编码模型配置未能读取，可重试或直接输入模型名称。"
          action={<Button size="small" onClick={() => { void bots.refetch(); inspections.forEach(query => void query.refetch()); }}>重试模型列表</Button>} />}
        <Text type="secondary">启动时冻结当前工作区，包含未提交源码；清理在隔离工作区完成，交由你审核并提交。</Text>
        <details><summary>高级参数</summary><Space wrap style={{ marginTop: 12 }}>
          <div className="code-health-field">推理强度<Select aria-label="推理强度" style={{ width: 170 }} value={effort} onChange={setEffort} disabled={starting}
            options={[
              { value: "minimal", label: "极低（minimal）" }, { value: "low", label: "低（low）" },
              { value: "medium", label: "中（medium）" }, { value: "high", label: "高（high）" },
              { value: "xhigh", label: "极高（xhigh）" }, { value: "max", label: "最高（max）" },
            ]} /></div>
          <div className="code-health-field">每组最多尝试<InputNumber aria-label="每组最多尝试" min={1} precision={0} value={attempts} onChange={setAttempts} disabled={starting} style={{ width: 120 }} /></div>
          <div className="code-health-field">总时限（小时）<InputNumber aria-label="总时限" min={0.1} precision={1} value={hours} onChange={setHours} disabled={starting} style={{ width: 140 }} /></div>
        </Space></details>
        <Space wrap><Button type="primary" loading={starting} disabled={!config.data || !(model.trim() || config.data.default_model)} onClick={() => void start()}>开始垃圾回收</Button>
          <Button onClick={() => setRulesOpen(true)}>查看黄金原则</Button>
          {config.data && <Text type="secondary">当前 HEAD：{config.data.base_commit.slice(0, 10)}</Text>}</Space>
        {error && <Alert type="error" content={error} />}
      </Space>
    </PageSection>
    <PageSection title="治理记录" description="每次运行保存源码基准、发现的问题与候选验证结果。">
      <Space wrap style={{ marginBottom: 12 }}><Input.Search aria-label="搜索治理任务" placeholder="搜索任务 ID" value={search}
        onChange={value => { setSearch(value); setPage(1); }} style={{ width: 260 }} />
        <Select aria-label="筛选治理状态" value={status} style={{ width: 210 }} onChange={value => { setStatus(value); setPage(1); }}
          options={[{ value: "", label: "全部状态" }, ...Object.entries(healthLabels).map(([value, label]) => ({ value, label }))]} />
        <Button onClick={() => void history.refetch()}>刷新</Button></Space>
      {history.isError && <Alert type="error" content={String(history.error)} />}
      <Table<HealthTask> rowKey="task_id" loading={history.isFetching} data={history.data?.tasks ?? []} scroll={{ x: 760 }}
        noDataElement={<Empty description="尚无治理记录，可从上方开始一次垃圾回收" />}
        pagination={{ current: page, pageSize: 20, total: history.data?.total ?? 0, onChange: setPage }} columns={[
          { title: "任务", dataIndex: "task_id", render: (_, row) => <Button type="text" onClick={() => openTask(row.task_id)}>{row.task_id.slice(0, 19)}</Button> },
          { title: "状态", render: (_, row) => <Tag>{healthStatus(row)}</Tag> },
          { title: "结果", render: (_, row) => healthSummary(row) },
          { title: "阶段", render: (_, row) => stageLabel(row.stage) },
          { title: "源码基准", render: (_, row) => <Text title={row.source.snapshot_digest}>{row.source.snapshot_digest.slice(0, 10)}</Text> },
          { title: "启动时间", render: (_, row) => new Date(row.created_at * 1000).toLocaleString() },
        ]} />
    </PageSection>
    <Drawer title="黄金原则" visible={rulesOpen} onCancel={() => setRulesOpen(false)} footer={null} width="min(100vw, 680px)">
      <Space direction="vertical" size={20}>{config.data?.rules.map(rule => <section key={rule.id}>
        <Text bold>{rule.title}</Text><p>{rule.guidance}</p><Text type="secondary">依据：{rule.reference} · 检测：{rule.detector}</Text>
      </section>)}</Space>
    </Drawer>
    <Drawer title="代码治理详情" visible={!!taskId} onCancel={() => openTask("")} footer={null} width="min(100vw, 1080px)" unmountOnExit>
      {taskId && <HealthDetail key={taskId} id={taskId} />}
    </Drawer>
  </div>;
}

function HealthDetail({ id }: { id: string }) {
  const client = useQueryClient();
  const [error, setError] = useState("");
  const [cancelling, setCancelling] = useState(false);
  const [logRef, setLogRef] = useState("");
  const query = useQuery({ queryKey: ["code-health-task", id], queryFn: ({ signal }) => healthApi.get(id, signal),
    retry: false, refetchInterval: state => ACTIVE.includes(state.state.data?.status ?? "") ? 2000 : false });
  const log = useQuery({ queryKey: ["code-health-log", id, logRef], enabled: !!logRef,
    queryFn: ({ signal }) => healthApi.log(id, logRef, signal), retry: false });
  const task = query.data;
  async function cancel() {
    setCancelling(true); setError("");
    try { await harnessApi.action(id, "cancel"); await query.refetch(); await client.invalidateQueries({ queryKey: ["code-health-history"] }); }
    catch (e) { setError(String(e)); } finally { setCancelling(false); }
  }
  if (!task) return query.isError ? <Alert type="error" content={String(query.error)} /> : <Spin tip="读取任务…" />;
  if (task.source.kind !== "code_health") return <Alert type="error" content="此记录不是代码治理任务" />;
  const findings = task.governance?.findings ?? [];
  const checkTable = (checks: Check[]) => <Table<Check> rowKey="log" size="small" pagination={false} data={checks} columns={[
    { title: "检查", dataIndex: "name" }, { title: "结果", render: (_, row) => row.exit_code === 0 ? "通过" : row.existing_failure ? "保留既有失败" : `未通过（${row.exit_code}）` },
    { title: "证据", render: (_, row) => <Button type="text" onClick={() => setLogRef(row.log)}>查看日志</Button> },
  ]} />;
  return <Space className="code-health-detail" direction="vertical" size={16} style={{ width: "100%" }}>
    <RepairProgress task={task} refreshTask={() => query.refetch()} title="治理进度" statusLabel={healthStatus(task)} />
    {query.isError && <Alert type="error" content={String(query.error)} />}
    {task.message && <Alert type={task.status === "fixed" && task.candidate_available ? "success" : "info"} content={task.message} />}
    {task.status === "fixed" && task.candidate_available === false && <Alert type="warning" content="候选已变化或不可读取，历史验收不能证明当前内容仍然有效。" />}
    {error && <Alert type="error" content={error} />}
    {ACTIVE.includes(task.status) && <Button status="danger" loading={cancelling} disabled={task.status === "cancel_requested"} onClick={() => void cancel()}>取消本次治理</Button>}
    <Descriptions column={1} size="small" data={[
      { label: "任务", value: task.task_id }, { label: "源码快照", value: task.source.snapshot_digest },
      { label: "基准提交", value: task.base_commit }, { label: "候选目录", value: task.worktree ?? "尚未创建" },
    ]} />
    <Alert type="info" content={healthSummary(task)} />
    <HealthLedger task={task} />
    <Tabs defaultActiveTab="findings">
      <Tabs.TabPane key="findings" title={`发现 ${findings.length}`}>
        {task.governance?.audit_summary && <p>{task.governance.audit_summary}</p>}
        {task.governance?.inspected_paths && <Text type="secondary">AI 报告读取了 {task.governance.inspected_paths.length} 个文件；此计数不代表全仓语义已验证。</Text>}
        <Table<Finding> rowKey="id" data={findings} scroll={{ x: 650 }} pagination={{ pageSize: 10 }} columns={[
          { title: "问题", dataIndex: "summary" }, { title: "位置", render: (_, row) => row.path ? `${row.path}${row.line ? `:${row.line}` : ""}` : "仓库检查" },
          { title: "处置", render: (_, row) => <Tag>{findingStatus(row, task)}</Tag> },
        ]} expandedRowRender={row => <div><p><b>依据：</b>{row.evidence}</p><p><b>建议：</b>{row.recommendation}</p><Text type="secondary">规则：{row.rule_id} · 来源：{row.detector}</Text></div>} />
      </Tabs.TabPane>
      <Tabs.TabPane key="candidate" title="候选与验证">
        <Space direction="vertical" size={20} style={{ width: "100%" }}>
          {!task.attempts?.length && <Empty description="尚未生成清理候选" />}
          {task.attempts?.map(attempt => <section key={attempt.number}>
            <Space wrap><Text bold>候选 #{attempt.number}{attempt.group_attempt ? ` · 组内第 ${attempt.group_attempt} 次` : ""}</Text><Tag>{attempt.status === "accepted" ? "验收通过" : attempt.status === "rejected" ? "未通过" : "处理中"}</Tag>
              {attempt.patch_sha256 && <a href={healthApi.patchUrl(id, attempt.number)} download>下载本次补丁</a>}</Space>
            {attempt.changed_files?.length && <pre>{attempt.changed_files.join("\n")}</pre>}
            {attempt.error && <Alert type="warning" content={attempt.error} />}
            {attempt.verification && <><p>仓库验收：{attempt.verification.profile}</p>{checkTable(attempt.verification.checks)}</>}
            {!!attempt.verification?.retained_failures?.length && <Alert type="info" content={`以下既有失败未扩大：${attempt.verification.retained_failures.join("、")}`} />}
            {attempt.review && <p>独立审查：{attempt.review.reason}</p>}
          </section>)}
        </Space>
      </Tabs.TabPane>
      <Tabs.TabPane key="checks" title="检查记录">{checkTable(task.check_logs?.length ? task.check_logs : task.governance?.before?.checks ?? [])}</Tabs.TabPane>
    </Tabs>
    <Drawer title="检查日志" visible={!!logRef} onCancel={() => setLogRef("")} footer={null} width="min(100vw, 860px)">
      {log.isError ? <Alert type="error" content={String(log.error)} /> : log.isPending ? <Spin /> :
        <>{log.data?.truncated && <Alert type="info" content="日志较长，展示末尾内容" />}<pre className="code-health-log">{log.data?.text}</pre></>}
    </Drawer>
  </Space>;
}
