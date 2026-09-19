import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Drawer, Empty, Input, InputNumber, Select, Space, Switch, Table, Tag, Typography } from "@arco-design/web-react";
import { api } from "../api";
import PageSection from "../shared/ui/PageSection";
import { governanceApi, RUN_LABELS, stopLabel, type GovernanceOptions, type GovernanceRun } from "../features/harness/governance";
import { GovernanceRunDetail } from "../features/harness/GovernanceRunDetail";
import { RepairDetail } from "../features/harness/RepairDetail";
import { repairModels } from "../features/harness/repairModels";

const { Text } = Typography;
const grid = { display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 210px), 1fr))", gap: 16 } as const;
const selectionFromHash = () => {
  const query = new URLSearchParams(window.location.hash.split("?")[1] ?? "");
  return { run: query.get("run") ?? "", task: query.get("task") ?? "" };
};

export default function CodeHealthPage() {
  const client = useQueryClient();
  const [mode, setMode] = useState<"time" | "findings">("time");
  const [hours, setHours] = useState(1);
  const [count, setCount] = useState(1);
  const [modelOverride, setModel] = useState("");
  const [effort, setEffort] = useState("medium");
  const [attempts, setAttempts] = useState(3);
  const [enabled, setEnabled] = useState(false);
  const [interval, setInterval] = useState(24);
  const [busy, setBusy] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [selection, setSelection] = useState(selectionFromHash);
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
  const valid = !!model.trim() && Number.isInteger(attempts) && attempts >= 1 && (mode === "time"
    ? Number.isFinite(hours) && Math.round(hours * 3600) >= 1 : Number.isInteger(count) && count >= 1);
  const options: GovernanceOptions = { model: model.trim(), reasoning_effort: effort, max_attempts: attempts,
    stop_condition: mode === "time" ? { mode, seconds: Math.round(hours * 3600) } : { mode, count } };
  const history = useQuery({ queryKey: ["governance-history", page, search, status],
    queryFn: ({ signal }) => governanceApi.runs(page, search, status, signal), retry: false, refetchInterval: 5000 });

  function loadOptions(saved: GovernanceOptions) {
    setModel(saved.model); setEffort(saved.reasoning_effort); setAttempts(saved.max_attempts);
    setMode(saved.stop_condition.mode);
    if (saved.stop_condition.mode === "time") setHours(saved.stop_condition.seconds / 3600);
    else setCount(saved.stop_condition.count);
  }
  useEffect(() => {
    if (!schedule.data || hydrated.current) return;
    hydrated.current = true;
    setEnabled(schedule.data.enabled); setInterval(schedule.data.interval_hours);
    if (schedule.data.options) loadOptions(schedule.data.options);
  }, [schedule.data]);
  useEffect(() => {
    const update = () => setSelection(selectionFromHash());
    window.addEventListener("hashchange", update);
    return () => window.removeEventListener("hashchange", update);
  }, []);
  function openRun(id: string) {
    setSelection({ run: id, task: "" });
    window.location.hash = id ? `code-health?run=${encodeURIComponent(id)}` : "code-health";
  }
  function restart(run: GovernanceRun) {
    hydrated.current = true;
    loadOptions(run.options);
    submitted.current = { body: "", requestId: "" };
    openRun("");
  }
  async function start() {
    if (!valid || busy) return;
    const identity = JSON.stringify(options);
    if (submitted.current.body !== identity) submitted.current = { body: identity, requestId: crypto.randomUUID() };
    setBusy(true); setError("");
    try {
      const run = await governanceApi.start({ ...options, request_id: submitted.current.requestId });
      submitted.current = { body: "", requestId: "" };
      await client.invalidateQueries({ queryKey: ["governance-history"] });
      openRun(run.run_id);
    } catch (value) { setError(value instanceof Error ? value.message : String(value)); }
    finally { setBusy(false); }
  }
  async function saveSchedule() {
    if (saving || (enabled && !valid) || !Number.isInteger(interval) || interval < 1) return;
    setSaving(true); setError("");
    try {
      const saved = await governanceApi.saveSchedule({ enabled, interval_hours: interval, options: valid ? options : null });
      client.setQueryData(["governance-schedule"], saved);
      setEnabled(saved.enabled);
    } catch (value) {
      setError(value instanceof Error ? value.message : String(value));
      const refreshed = await schedule.refetch();
      if (refreshed.data) setEnabled(refreshed.data.enabled);
    } finally { setSaving(false); }
  }
  return <Space direction="vertical" size={20} style={{ width: "100%", minWidth: 0 }}>
    <PageSection title="代码熵回收" description="依据黄金原则与现行 SDD，发现一个问题后立即修复、验证并交付 PR；合并后再发现下一项。">
      <Space direction="vertical" size={16} style={{ width: "100%" }}>
        <div style={grid}>
          <div>停止条件<Select aria-label="熵回收停止条件" value={mode} onChange={setMode}
            options={[{ value: "time", label: "按时间" }, { value: "findings", label: "按问题发现数" }]} /></div>
          {mode === "time" ? <div>累计执行时长（小时）<InputNumber aria-label="熵回收累计执行小时" min={1 / 60} value={hours} onChange={setHours} /></div>
            : <div>问题发现数上限<InputNumber aria-label="熵回收问题发现数" min={1} precision={0} value={count} onChange={setCount} /></div>}
        </div>
        <div style={grid}>
          <div>回收模型<Select aria-label="熵回收模型" allowCreate showSearch value={model || undefined}
            placeholder="选择或填写模型名称" options={models.options} onChange={setModel} /></div>
          <div>推理强度<Select aria-label="熵回收推理强度" value={effort} onChange={setEffort}
            options={["minimal", "low", "medium", "high", "xhigh", "max"]} /></div>
          <div>每个问题的修复尝试上限（含首轮）<InputNumber aria-label="每个问题的修复尝试上限" min={1} precision={0} value={attempts} onChange={setAttempts} /></div>
        </div>
        <Text type="secondary">修复尝试上限只限制同一问题的首次修复和失败返工。{mode === "time"
          ? "累计执行时间跨问题共享，等待 GitHub 检查或审查不计时。" : "达到发现数后完成最后一项再结束；此模式不附加时间上限。"}</Text>
        <Text type="secondary">每项从最新远端 main 调查，本地未提交改动不纳入。现有测试、依赖和规则调整列为待判断；当前问题未完成时停止继续发现。</Text>
        {(error || config.isError) && <Alert type="error" content={error || String(config.error)} />}
        <Button type="primary" loading={busy} disabled={!valid || config.isPending || config.isError}
          onClick={() => void start()}>开始逐项熵回收并自动交付 PR</Button>
      </Space>
    </PageSection>
    <PageSection title="可选定时" description="默认关闭。启用后使用上方的模型、停止条件和每项修复尝试上限；已有活动回收或未完成交付时跳过本次触发。">
      <Space direction="vertical" style={{ width: "100%" }}>
        {schedule.isError && <Alert type="error" content={String(schedule.error)} />}
        {schedule.data?.last_error && <Alert type="warning" content={schedule.data.last_error} />}
        <Space wrap>
          <Switch aria-label="启用定时熵回收" checked={enabled} onChange={setEnabled} disabled={schedule.isPending || saving} />
          <Text>每隔</Text><InputNumber aria-label="熵回收间隔小时" min={1} precision={0} value={interval} onChange={setInterval} /><Text>小时</Text>
          <Button loading={saving} disabled={schedule.isPending || (enabled && !valid) || !Number.isInteger(interval) || interval < 1}
            onClick={() => void saveSchedule()}>保存定时设置</Button>
        </Space>
        <Text type="secondary">保存状态：{schedule.data ? schedule.data.enabled ? "已启用" : "关闭" : "未读取"}；启用后约一分钟首次触发。关闭定时不取消已经开始的回收。</Text>
        {schedule.data?.enabled && schedule.data.timer?.ActiveState !== "active" && <Alert type="warning" content="定时器运行状态尚未确认，请检查调度状态或重新保存设置。" />}
        {schedule.data?.last_run && <Text>最近触发：{({ created: "已创建回收批次", skipped: "已有回收，已跳过", failed: "触发失败", dispatching: "正在创建" } as Record<string, string>)[schedule.data.last_run.status] ?? schedule.data.last_run.status}
          {schedule.data.last_run.run_id && <Button type="text" onClick={() => openRun(schedule.data!.last_run!.run_id!)}>查看批次</Button>}</Text>}
      </Space>
    </PageSection>
    <PageSection title="熵回收记录" description="按回收批次查看累计进度，展开后查看每个问题的证据、验证和 PR。">
      <Space wrap style={{ marginBottom: 12 }}>
        <Input.Search aria-label="搜索熵回收批次" placeholder="搜索批次 ID" value={search} onChange={value => { setSearch(value); setPage(1); }} />
        <Select aria-label="熵回收状态" value={status} onChange={value => { setStatus(value); setPage(1); }}
          options={[{ value: "", label: "全部状态" }, ...Object.entries(RUN_LABELS).map(([value, label]) => ({ value, label }))]} />
      </Space>
      {history.isError && <Alert type="error" content={String(history.error)} />}
      <Table<GovernanceRun> rowKey="run_id" loading={history.isPending} data={history.data?.runs ?? []} scroll={{ x: 820 }}
        noDataElement={<Empty description="尚无熵回收批次" />} pagination={{ current: page, pageSize: 20, total: history.data?.total ?? 0, onChange: setPage }}
        columns={[
          { title: "批次", width: 180, render: (_, run) => <Button type="text" onClick={() => openRun(run.run_id)}>{run.run_id.slice(0, 17)}</Button> },
          { title: "停止条件", render: (_, run) => stopLabel(run.options.stop_condition) },
          { title: "进度", render: (_, run) => `发现 ${run.found_count} · 合并 ${run.merged_count}` },
          { title: "当前问题", render: (_, run) => run.tasks[run.tasks.length - 1]?.governance_summary?.topic ?? "等待调查" },
          { title: "状态", render: (_, run) => <Tag>{RUN_LABELS[run.status] ?? run.status}</Tag> },
          { title: "累计执行", render: (_, run) => `${Math.round(run.elapsed_seconds)} 秒` },
        ]} />
    </PageSection>
    <Drawer title={selection.run ? "代码熵回收批次" : "代码熵回收历史任务"} visible={!!(selection.run || selection.task)} onCancel={() => openRun("")}
      footer={null} width="min(100vw, 1080px)" unmountOnExit>
      {selection.run ? <GovernanceRunDetail key={selection.run} runId={selection.run} onRestart={restart} />
        : selection.task && <RepairDetail key={selection.task} taskId={selection.task} managedRun onRestart={() => openRun("")} />}
    </Drawer>
  </Space>;
}
