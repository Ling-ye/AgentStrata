import { applicationHref, applicationLocation } from "../features/applications/navigation";
import { useHarnessClient } from "../features/harness/useHarnessClient";
import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Card, Drawer, Input, InputNumber, Select, Space, Spin, Table, Tag, Typography } from "@arco-design/web-react";
import PageSection from "../shared/ui/PageSection";
import { REPAIR_LABELS, deliveryLabel, repairStatusLabel, sourceLabel, stageLabel, type RepairTask, type SourceKind, type SourcePreview, type StartRepair } from "../features/harness/api";
import { caseInstanceId, selectedInstance } from "../features/harness/caseInstance";
import { RepairDetail } from "../features/harness/RepairDetail";
import ModelProfileSelect from "../features/llm/ModelProfileSelect";
import { llmApi, profileOptions } from "../features/llm/api";

const { Text } = Typography;
const taskFromHash = () => new URLSearchParams(window.location.hash.split("?")[1] ?? "").get("task") ?? "";

export default function HarnessPage({ initialApplication = "" }: { initialApplication?: string }) {
  const { harnessApi, id: appId } = useHarnessClient();
  const client = useQueryClient();
  const route = new URLSearchParams(window.location.hash.split("?")[1]);
  const linkedKind: SourceKind = route.get("sourceKind") === "robot_task" ? "robot_task" : "evaluation";
  const linkedId = appId ? route.get("sourceId") || "" : "";
  const [taskKind, setTaskKind] = useState("repair");
  const [kind, setKind] = useState<SourceKind>(linkedKind);
  const [sourceId, setSourceId] = useState(linkedId);
  const [botId, setBotId] = useState(initialApplication);
  const [preview, setPreview] = useState<SourcePreview>();
  const [loadedId, setLoadedId] = useState("");
  const [loading, setLoading] = useState(false);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState("");
  const [repairHint, setRepairHint] = useState("");
  const [expectedBehavior, setExpectedBehavior] = useState("");
  const [selectedModel, setModel] = useState("");
  const [attempts, setAttempts] = useState(3);
  const [hours, setHours] = useState(1);
  const [taskId, setTaskId] = useState(taskFromHash);
  const [page, setPage] = useState(1);
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("");
  const generation = useRef(0);
  const submitted = useRef({ body: "", requestId: "" });
  const inspection = useQuery({ queryKey: ["llm-config"], queryFn: ({ signal }) => llmApi.config(signal), retry: false });
  const models = { options: profileOptions(inspection.data, true), error: "", defaultModel: inspection.data?.bindings.harness ?? "" };
  const model = selectedModel || models.defaultModel;
  const history = useQuery({ queryKey: ["harness-history", page, search, status, taskKind],
    queryFn: ({ signal }) => harnessApi.history(page, search, status, signal, taskKind), retry: false, refetchInterval: 5000 });
  useEffect(() => {
    const update = () => setTaskId(taskFromHash());
    window.addEventListener("hashchange", update);
    return () => window.removeEventListener("hashchange", update);
  }, []);
  function openTask(id: string) {
    const current = applicationLocation(window.location.hash);
    if (appId && (current.id !== appId || current.tab !== "repairs")) return;
    setTaskId(id);
    if (appId) {
      const params = new URLSearchParams(window.location.hash.split("?")[1]);
      if (id) params.set("task", id); else params.delete("task");
      window.location.hash = "applications?" + params;
      return;
    }
    window.location.hash = id ? `harness?task=${encodeURIComponent(id)}` : "harness";
  }
  async function load(sourceKind = kind, sourceValue = sourceId, sourceBot = botId, resetModel = true) {
    if (starting || !sourceValue.trim() || (sourceKind === "robot_task" && !sourceBot)) return;
    const current = ++generation.current;
    setLoading(true); setError(""); setPreview(undefined); setLoadedId("");
    if (resetModel) setModel("");
    try {
      const id = sourceKind === "evaluation" ? caseInstanceId(sourceValue) : sourceValue.trim();
      const value = await harnessApi.load(sourceKind, id, sourceKind === "robot_task" ? sourceBot : "");
      if (current !== generation.current) return;
      if (sourceKind === "evaluation" && !selectedInstance(value, id)) throw new Error("返回的 Case 实例与输入 ID 不一致，请重新加载来源");
      setPreview(value);
      setLoadedId(id);
    } catch (value) { if (current === generation.current) setError(value instanceof Error ? value.message : String(value)); }
    finally { if (current === generation.current) setLoading(false); }
  }
  useEffect(() => {
    if (linkedId) { setKind(linkedKind); setSourceId(linkedId); void load(linkedKind, linkedId, initialApplication); }
    return () => { generation.current++; };
  }, [linkedId, linkedKind, initialApplication]);
  function restart(task: RepairTask) {
    if (task.source.kind === "code_health" || task.source.kind === "application") return;
    const sourceKind = task.source.kind ?? "evaluation";
    const id = sourceKind === "robot_task" ? task.source.run_id! : task.source.case_instance_id!;
    setKind(sourceKind); setSourceId(id); setBotId(task.source.bot_id ?? "");
    setRepairHint(task.source.feedback?.repair_hint ?? "");
    setExpectedBehavior(task.source.feedback?.expected_behavior ?? "");
    setModel(task.options.model_profile ?? "");
    setAttempts(task.options.max_attempts); setHours((task.options.timeout_seconds ?? 3600) / 3600);
    submitted.current = { body: "", requestId: "" };
    openTask("");
    void load(sourceKind, id, task.source.bot_id ?? "", false);
  }
  async function start() {
    if (!preview || preview.blockers.length || starting || loading || inspection.isFetching || inspection.isError || models.error || !models.options.some(option => option.value === model)) return;
    const trial = selectedInstance(preview, loadedId);
    if (kind === "evaluation" && (!trial || !["failed", "error"].includes(trial.outcome) || preview.blockers.length)) return;
    const body = { source_kind: kind, ...(kind === "evaluation" ? {
      case_instance_id: trial!.case_instance_id,
    } : { bot_id: preview.bot_id, run_id: preview.run_id }),
      ...((repairHint.trim() || (kind === "robot_task" && expectedBehavior.trim())) ? { feedback: {
        ...(repairHint.trim() ? { repair_hint: repairHint } : {}),
        ...(kind === "robot_task" && expectedBehavior.trim() ? { expected_behavior: expectedBehavior } : {}),
      } } : {}), profile: model.trim(),
      max_attempts: attempts, timeout_seconds: Math.round(hours * 3600) };
    const identity = JSON.stringify(body);
    if (submitted.current.body !== identity) submitted.current = { body: identity, requestId: crypto.randomUUID() };
    setStarting(true); setError("");
    try {
      const result = await harnessApi.start({ ...body, request_id: submitted.current.requestId } as StartRepair);
      submitted.current = { body: "", requestId: "" };
      client.setQueryData(["harness-task", result.task_id], result);
      openTask(result.task_id);
      await client.invalidateQueries({ queryKey: ["harness-history"] });
    } catch (value) { setError(value instanceof Error ? value.message : String(value)); }
    finally { setStarting(false); }
  }
  const blocked = !!preview?.blockers.length;
  const trial = selectedInstance(preview, loadedId);
  const sourceLink = kind === "robot_task"
    ? applicationHref(appId,"records",{run:sourceId})
    : applicationHref(appId,"evaluations",{evaluation:preview?.evaluation_id || route.get("evaluation") || "",caseId:sourceId});
  return <PageSection title="Harness 修复">
    <Space direction="vertical" size={20} style={{ width: "100%" }}>
      {!sourceId && <Card title="选择修复来源"><Space wrap>
        <Button href={applicationHref(appId,"records")}>从业务任务选择</Button>
        <Button href={applicationHref(appId,"evaluations")}>从测评结果选择</Button>
      </Space></Card>}
      {sourceId && <Card title={kind === "evaluation" ? "修复失败用例" : "修复业务任务"} extra={<Button size="small" href={sourceLink}>查看原始记录</Button>}>
        <Space direction="vertical" size={14} style={{ width: "100%" }}>
          {loading && <Spin tip="正在核对来源证据…" />}
          {error && <Alert type="error" content={error} action={<Button onClick={()=>void load()}>重新核对</Button>} />}
          {preview && <>
            {blocked && <Alert type="warning" content={preview.blockers.join("；")} />}
            {kind === "robot_task" && <Text>业务任务 · {preview.run_id}</Text>}
            {!!preview.warnings?.length && <Alert type="warning" title="来源证据缺口 · 可开始诊断"
              content={preview.warnings.map(warning => warning.message).join("；")} />}
            {kind === "evaluation" && trial && <div aria-label="待修复 Case 详情" style={{ overflowWrap: "anywhere" }}>
              <Text bold>{trial.case_ref}</Text> <Tag color="red">{trial.outcome === "failed" ? "失败" : trial.outcome === "error" ? "执行错误" : trial.outcome}</Tag>
              <div>{trial.target_id} · 第 {trial.attempt} 次执行</div>
              <details><summary>来源标识</summary><div>Case 实例 · {trial.case_instance_id}</div><div>所属测评 · {trial.evaluation_id}</div></details>
            </div>}
            {!!preview.history.length && <details><summary>关联历史修复（{preview.history.length}）</summary><Space direction="vertical">
              {preview.history.map(item => <Button type="text" key={item.task_id} onClick={() => openTask(item.task_id)}>{repairStatusLabel(item)} · {sourceLabel(item)}</Button>)}</Space></details>}
            {preview.evidence && <details><summary>查看任务证据</summary><pre style={{ maxHeight: 320, overflow: "auto", whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{JSON.stringify(preview.evidence, null, 2)}</pre></details>}
            <Space direction="vertical" size={12} style={{ width: "100%" }}>
              <div><Text>修复提示（可选）</Text><Input.TextArea aria-label="修复提示" value={repairHint} disabled={starting}
                onChange={setRepairHint} autoSize={{ minRows: 3, maxRows: 8 }} placeholder="描述问题、补充背景或提供调查线索" /></div>
              {kind === "robot_task" && <div><Text>参考答案／预期行为（可选）</Text><Input.TextArea aria-label="参考答案／预期行为" value={expectedBehavior} disabled={starting}
                onChange={setExpectedBehavior} autoSize={{ minRows: 4, maxRows: 12 }} placeholder="填写期望的答案或行为，也可以附上解释和来源线索" /></div>}

            </Space>
            {(!blocked || kind === "robot_task") && <>
              <details><summary>模型与预算 · {model || "待选择"} · 最多 {attempts} 次候选 · {hours} 小时</summary>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 210px), 1fr))", gap: 16 }}>
                <div>修复方案<ModelProfileSelect purpose="harness" value={selectedModel} onChange={setModel} worker disabled={starting} /></div>
                <div>最多候选次数<InputNumber aria-label="最多候选次数" min={1} precision={0} value={attempts} disabled={starting} onChange={setAttempts} style={{ width: "100%" }} /></div>
                <div>总时间预算（小时）<InputNumber aria-label="修复时间预算" min={0.01} step={0.5} precision={2} value={hours} disabled={starting} onChange={setHours} style={{ width: "100%" }} /></div>
              </div>
              {!inspection.isFetching && (inspection.isError || models.error) && <Alert type="error" content={inspection.isError ? "机器人模型配置读取失败" : models.error}
                action={<Button size="small" onClick={() => void inspection.refetch()}>重试</Button>} />}
              {!inspection.isFetching && !inspection.isError && !models.error && !!selectedModel && !models.options.some(option => option.value === selectedModel) &&
                <Alert type="warning" content="原修复模型当前不可用，请重新选择模型。" />}
              <Text type="secondary">真实 Agent 每个验证 Case 最多 {(2 * attempts + 1) * Math.max(3, preview.repetitions ?? 3)} 次执行（基线、候选与独立确认）；完整数量在验证计划冻结后展示，评分和回归共用总预算。</Text></details>
              <Text type="secondary">从冻结的 main 创建候选。独立验收通过后提交 PR，CI 通过后自动合并。</Text>
              <Button type="primary" loading={starting} disabled={blocked || loading || inspection.isFetching || inspection.isError || !!models.error || !models.options.some(option => option.value === model) || (kind === "evaluation" && (!trial || !["failed", "error"].includes(trial.outcome)))} onClick={() => void start()}>
                开始修复并自动交付 PR</Button>
            </>}
          </>}
        </Space>
      </Card>}
      <Card title="本应用的修复记录" extra={<Button onClick={() => void history.refetch()}>刷新历史</Button>}>
        <Space wrap style={{ marginBottom: 16 }}><Input aria-label="搜索修复历史" placeholder="搜索修复、Case 实例、测评或任务 ID" value={search} allowClear
          onChange={value => { setSearch(value); setPage(1); }} />
          <Select aria-label="任务历史类型" value={taskKind} style={{ width: 180 }} onChange={value => { setTaskKind(value); setPage(1); }} options={[{ value: "repair", label: "全部需求与修复" }, { value: "application", label: "应用接入与改进" }]} />
          <Select aria-label="修复历史状态" value={status} style={{ width: 180 }} onChange={value => { setStatus(value); setPage(1); }} options={[
            { label: "全部状态", value: "" }, ...Object.entries(REPAIR_LABELS).map(([value, label]) => ({ value, label })),
          ]} /></Space>
        {history.isError && <Alert type="error" content={String(history.error)} />}
        <Table size="small" rowKey="task_id" loading={history.isPending} data={history.data?.tasks ?? []} scroll={{ x: 900 }}
          pagination={{ current: page, pageSize: 20, total: history.data?.total ?? 0, onChange: setPage }} columns={[
            { title: "来源", width: 340, render: (_, task) => <Button type="text" onClick={() => openTask(task.task_id)} style={{ whiteSpace: "normal", height: "auto", textAlign: "left", overflowWrap: "anywhere" }}>{sourceLabel(task)}</Button> },
            { title: "状态", render: (_, task) => <Tag color={task.status === "fixed" ? "green" : "blue"}>{repairStatusLabel(task)}</Tag> },
            { title: "源码交付", render: (_, task) => task.archived ? "只读归档" : task.delivery ? <Tag>{deliveryLabel(task.delivery.state)}</Tag> : "历史记录" },
            { title: "阶段", render: (_, task) => stageLabel(task.stage) },
            { title: "更新时间", render: (_, task) => new Date(task.updated_at * 1000).toLocaleString() },
            { title: "操作", render: (_, task) => <Button size="small" onClick={() => openTask(task.task_id)}>查看记录</Button> },
          ]} />
      </Card>
    </Space>
    <Drawer title="AI Harness 任务记录" visible={!!taskId} escToExit width="min(1000px, 100vw)" footer={null} onCancel={() => openTask("")}>
      {taskId && <RepairDetail key={taskId} taskId={taskId} onRestart={restart} onSelect={openTask} />}
    </Drawer>
  </PageSection>;
}
