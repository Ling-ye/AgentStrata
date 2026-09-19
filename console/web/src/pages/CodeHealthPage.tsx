import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Drawer, Empty, Input, InputNumber, Select, Space, Switch, Table, Tag, Typography } from "@arco-design/web-react";
import { api } from "../api";
import PageSection from "../shared/ui/PageSection";
import { harnessApi, REPAIR_LABELS, repairStatusLabel, stageLabel, deliveryLabel, type RepairTask } from "../features/harness/api";
import { governanceApi } from "../features/harness/governance";
import { RepairDetail } from "../features/harness/RepairDetail";
import { repairModels } from "../features/harness/repairModels";

const { Text } = Typography;
const grid = { display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 210px), 1fr))", gap: 16 } as const;
const taskFromHash = () => new URLSearchParams(window.location.hash.split("?")[1] ?? "").get("task") ?? "";

export default function CodeHealthPage() {
  const client = useQueryClient();
  const [modelOverride, setModel] = useState("");
  const [effort, setEffort] = useState("medium");
  const [attempts, setAttempts] = useState(3);
  const [hours, setHours] = useState(1);
  const [singleIssue, setSingleIssue] = useState(false);
  const [hint, setHint] = useState("");
  const [enabled, setEnabled] = useState(false);
  const [interval, setInterval] = useState(24);
  const [busy, setBusy] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [taskId, setTaskId] = useState(taskFromHash);
  const [page, setPage] = useState(1);
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("");
  const hydrated = useRef(false);
  const submitted = useRef({ body: "", requestId: "" });
  const config = useQuery({ queryKey: ["governance-config"], queryFn: ({ signal }) => governanceApi.config(signal), retry: false });
  const schedule = useQuery({ queryKey: ["governance-schedule"], queryFn: ({ signal }) => governanceApi.schedule(signal), retry: false });
  const bots = useQuery({ queryKey: ["bots"], queryFn: api.listBots });
  const defaultBot = bots.data?.[0]?.instance_id;
  const inspection = useQuery({ queryKey: ["inspection", defaultBot],
    queryFn: ({ signal }) => api.inspection(defaultBot!, undefined, undefined, signal), enabled: !!defaultBot, retry: false });
  const models = repairModels(inspection.data?.current);
  const model = modelOverride || config.data?.default_model || models.defaultModel;
  const valid = !!model.trim() && Number.isInteger(attempts) && attempts >= 1 && hours > 0;
  const options = { model: model.trim(), reasoning_effort: effort, max_attempts: attempts,
    timeout_seconds: Math.round(hours * 3600), single_issue: singleIssue };
  const history = useQuery({ queryKey: ["governance-history", page, search, status],
    queryFn: ({ signal }) => harnessApi.history(page, search, status, signal, "code_health"), retry: false, refetchInterval: 5000 });

  useEffect(() => {
    if (!schedule.data || hydrated.current) return;
    hydrated.current = true;
    setEnabled(schedule.data.enabled); setInterval(schedule.data.interval_hours); setHint(schedule.data.repair_hint);
    if (schedule.data.options) {
      const saved = schedule.data.options;
      setModel(saved.model); setEffort(saved.reasoning_effort); setAttempts(saved.max_attempts); setHours(saved.timeout_seconds / 3600);
      setSingleIssue(saved.single_issue ?? false);
    }
  }, [schedule.data]);
  useEffect(() => {
    const update = () => setTaskId(taskFromHash());
    window.addEventListener("hashchange", update);
    return () => window.removeEventListener("hashchange", update);
  }, []);
  function openTask(id: string) {
    setTaskId(id);
    window.location.hash = id ? `code-health?task=${encodeURIComponent(id)}` : "code-health";
  }
  function restart(task: RepairTask) {
    setModel(task.options.model); setEffort(task.options.reasoning_effort);
    setAttempts(task.options.max_attempts); setHours(task.options.timeout_seconds / 3600);
    setSingleIssue(task.options.single_issue ?? false);
    setHint(task.source.feedback?.repair_hint ?? "");
    submitted.current = { body: "", requestId: "" };
    openTask("");
  }
  async function start() {
    if (!valid || busy) return;
    const body = { source_kind: "code_health" as const, ...options, ...(hint.trim() ? { feedback: { repair_hint: hint } } : {}) };
    const identity = JSON.stringify(body);
    if (submitted.current.body !== identity) submitted.current = { body: identity, requestId: crypto.randomUUID() };
    setBusy(true); setError("");
    try {
      const task = await harnessApi.start({ ...body, request_id: submitted.current.requestId });
      submitted.current = { body: "", requestId: "" };
      await client.invalidateQueries({ queryKey: ["governance-history"] });
      openTask(task.task_id);
    } catch (value) { setError(value instanceof Error ? value.message : String(value)); }
    finally { setBusy(false); }
  }
  async function saveSchedule() {
    if (saving || (enabled && !valid) || !Number.isInteger(interval) || interval < 1) return;
    setSaving(true); setError("");
    try {
      const saved = await governanceApi.saveSchedule({ enabled, interval_hours: interval,
        options: valid ? options : null, repair_hint: hint });
      client.setQueryData(["governance-schedule"], saved);
      setEnabled(saved.enabled);
    } catch (value) {
      setError(value instanceof Error ? value.message : String(value));
      const refreshed = await schedule.refetch();
      if (refreshed.data) setEnabled(refreshed.data.enabled);
    } finally { setSaving(false); }
  }
  return <Space direction="vertical" size={20} style={{ width: "100%", minWidth: 0 }}>
    <PageSection title="代码熵回收" description="依据 SDD 与黄金原则自主发现并清理代码熵，一次完成一个连贯主题，经验证和独立审查后自动交付 PR。">
      <Space direction="vertical" size={16} style={{ width: "100%" }}>
        <Text type="secondary">调查范围为远端 main 的全仓源码，本地未提交改动不纳入。现有测试、依赖和规则调整会列为待判断。</Text>
        <div style={grid}>
          <div>回收模型<Select aria-label="熵回收模型" allowCreate showSearch value={model || undefined}
            placeholder="选择或填写模型名称" options={models.options} onChange={setModel} /></div>
          <div>推理强度<Select aria-label="熵回收推理强度" value={effort} onChange={setEffort}
            options={["minimal", "low", "medium", "high", "xhigh", "max"]} /></div>
          <div>轮次预算（含首轮）<InputNumber aria-label="熵回收轮次预算" min={1} precision={0} value={attempts} onChange={setAttempts} /></div>
          <div>累计执行预算（小时）<InputNumber aria-label="熵回收时间预算" min={1 / 60} value={hours} onChange={setHours} /></div>
        </div>
        <Space wrap><Switch aria-label="单问题模式" checked={singleIssue} onChange={setSingleIssue} />
          <Text>单问题模式：确认第一个有证据的问题后停止继续发现，只修复这一项并结束。</Text></Space>
        <div>回收提示（可选）<Input.TextArea aria-label="熵回收提示" value={hint} onChange={setHint}
          autoSize={{ minRows: 2, maxRows: 8 }} placeholder="可提示关注某个领域；不填写则由 Agent 自主调查。" /></div>
        {(error || config.isError) && <Alert type="error" content={error || String(config.error)} />}
        <Button type="primary" loading={busy} disabled={!valid || config.isPending || config.isError}
          onClick={() => void start()}>开始熵回收并自动交付 PR</Button>
      </Space>
    </PageSection>
    <PageSection title="可选定时" description="默认关闭。启用后使用上方的模型、预算、单问题模式和回收提示；已有熵回收任务或待完成 PR 时跳过本次触发。">
      <Space direction="vertical" style={{ width: "100%" }}>
        {schedule.isError && <Alert type="error" content={String(schedule.error)} />}
        {schedule.data?.last_error && <Alert type="warning" content={schedule.data.last_error} />}
        <Space wrap>
          <Switch aria-label="启用定时熵回收" checked={enabled} onChange={setEnabled} disabled={schedule.isPending || schedule.isError || saving} />
          <Text>每隔</Text><InputNumber aria-label="熵回收间隔小时" min={1} precision={0} value={interval} onChange={setInterval} /><Text>小时</Text>
          <Button loading={saving} disabled={schedule.isPending || schedule.isError || (enabled && !valid)} onClick={() => void saveSchedule()}>保存定时设置</Button>
        </Space>
        <Text type="secondary">保存状态：{schedule.data?.enabled ? "已启用" : "关闭"}；启用后约一分钟首次触发。关闭定时不取消已经开始的任务。</Text>
        {schedule.data?.enabled && schedule.data.timer?.ActiveState !== "active" && <Alert type="warning" content="定时器运行状态尚未确认，请检查调度状态或重新保存设置。" />}
        {schedule.data?.last_run && <Text>最近触发：{({ created: "已创建熵回收任务", skipped: "已有熵回收任务，已跳过", failed: "触发失败", dispatching: "正在创建" } as Record<string, string>)[schedule.data.last_run.status] ?? schedule.data.last_run.status}
          {schedule.data.last_run.task_id && <Button type="text" onClick={() => openTask(schedule.data!.last_run!.task_id!)}>查看任务</Button>}</Text>}
      </Space>
    </PageSection>
    <PageSection title="熵回收记录" description="查看回收主题、调查证据、验证和交付结果。">
      <Space wrap style={{ marginBottom: 12 }}>
        <Input.Search aria-label="搜索熵回收任务" placeholder="搜索任务 ID" value={search} onChange={value => { setSearch(value); setPage(1); }} />
        <Select aria-label="熵回收状态" value={status} onChange={value => { setStatus(value); setPage(1); }}
          options={[{ value: "", label: "全部状态" }, ...Object.entries(REPAIR_LABELS).filter(([key]) => !["waiting_input", "not_reproduced"].includes(key)).map(([value, label]) => ({ value, label }))]} />
      </Space>
      {history.isError && <Alert type="error" content={String(history.error)} />}
      <Table<RepairTask> rowKey="task_id" loading={history.isPending} data={history.data?.tasks ?? []} scroll={{ x: 760 }}
        noDataElement={<Empty description="尚无熵回收记录" />} pagination={{ current: page, pageSize: 20, total: history.data?.total ?? 0, onChange: setPage }}
        columns={[
          { title: "任务", width: 200, render: (_, task) => <Button type="text" onClick={() => openTask(task.task_id)}>{task.task_id.slice(0, 19)}</Button> },
          { title: "回收主题", render: (_, task) => task.governance_summary?.topic ?? task.governance_summary?.summary ?? "等待调查" },
          { title: "状态", width: 130, render: (_, task) => <Tag>{repairStatusLabel(task)}</Tag> },
          { title: "阶段", width: 150, render: (_, task) => stageLabel(task.stage) },
          { title: "交付", width: 160, render: (_, task) => task.delivery ? deliveryLabel(task.delivery.state) : "尚无交付" },
        ]} />
    </PageSection>
    <Drawer title="代码熵回收详情" visible={!!taskId} onCancel={() => openTask("")} footer={null} width="min(100vw, 1080px)" unmountOnExit>
      {taskId && <RepairDetail key={taskId} taskId={taskId} onRestart={restart} />}
    </Drawer>
  </Space>;
}
