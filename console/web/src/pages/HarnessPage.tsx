import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Card, Checkbox, Drawer, Input, InputNumber, Select, Space, Table, Tag, Typography } from "@arco-design/web-react";
import { api } from "../api";
import PageSection from "../shared/ui/PageSection";
import { harnessApi, REPAIR_LABELS, repairStatusLabel, sourceLabel, stageLabel,
  type SourceKind, type SourcePreview, type StartRepair } from "../features/harness/api";
import { caseInstanceId, selectedInstance } from "../features/harness/caseInstance";
import { RepairDetail } from "../features/harness/RepairDetail";

const { Text } = Typography;
const taskFromHash = () => new URLSearchParams(window.location.hash.split("?")[1] ?? "").get("task") ?? "";

export default function HarnessPage() {
  const client = useQueryClient();
  const [kind, setKind] = useState<SourceKind>("evaluation");
  const [sourceId, setSourceId] = useState("");
  const [botId, setBotId] = useState("");
  const [preview, setPreview] = useState<SourcePreview>();
  const [loadedId, setLoadedId] = useState("");
  const [loading, setLoading] = useState(false);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState("");
  const [model, setModel] = useState("");
  const [reviewAndCommit, setReviewAndCommit] = useState(true);
  const [effort, setEffort] = useState("medium");
  const [attempts, setAttempts] = useState(3);
  const [seconds, setSeconds] = useState(7200);
  const [taskId, setTaskId] = useState(taskFromHash);
  const [page, setPage] = useState(1);
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("");
  const generation = useRef(0);
  const submitted = useRef({ body: "", requestId: "" });
  const bots = useQuery({ queryKey: ["bots"], queryFn: api.listBots });
  const history = useQuery({ queryKey: ["harness-history", page, search, status],
    queryFn: ({ signal }) => harnessApi.history(page, search, status, signal), retry: false, refetchInterval: 5000 });
  useEffect(() => {
    const update = () => setTaskId(taskFromHash());
    window.addEventListener("hashchange", update);
    return () => window.removeEventListener("hashchange", update);
  }, []);
  function openTask(id: string) {
    setTaskId(id);
    window.location.hash = id ? `harness?task=${encodeURIComponent(id)}` : "harness";
  }
  function resetPreview() { generation.current++; setPreview(undefined); setLoadedId(""); setError(""); setLoading(false); }
  async function load() {
    if (starting || !sourceId.trim() || (kind === "robot_task" && !botId)) return;
    const current = ++generation.current;
    setLoading(true); setError(""); setPreview(undefined); setLoadedId("");
    try {
      const id = kind === "evaluation" ? caseInstanceId(sourceId) : sourceId.trim();
      const value = await harnessApi.load(kind, id, kind === "robot_task" ? botId : "");
      if (current !== generation.current) return;
      if (kind === "evaluation" && !selectedInstance(value, id)) throw new Error("返回的 Case 实例与输入 ID 不一致，请重新加载来源");
      setPreview(value);
      setLoadedId(id);
    } catch (value) { if (current === generation.current) setError(value instanceof Error ? value.message : String(value)); }
    finally { if (current === generation.current) setLoading(false); }
  }
  async function start() {
    if (!preview || starting || loading) return;
    const trial = selectedInstance(preview, loadedId);
    if (kind === "evaluation" && (!trial || !["failed", "error"].includes(trial.outcome) || preview.blockers.length)) return;
    const body = { source_kind: kind, ...(kind === "evaluation" ? {
      case_instance_id: trial!.case_instance_id,
    } : { bot_id: preview.bot_id, run_id: preview.run_id }), model: model.trim(), reasoning_effort: effort,
      max_attempts: attempts, timeout_seconds: seconds, review_and_commit: reviewAndCommit };
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
  return <PageSection title="AI Harness 修复" description="加载一个失败来源，完成自检、隔离修复和回归验证。每次处理一个问题。">
    <Space direction="vertical" size={20} style={{ width: "100%" }}>
      <Card title="发起修复">
        <Space direction="vertical" size={16} style={{ width: "100%" }}>
          <Text type="secondary">测评中心负责运行测评，机器人任务流负责查看运行过程；修复过程和历史统一保存在此处。</Text>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 240px), 1fr))", gap: 16 }}>
            <div>来源类型<Select aria-label="修复来源类型" value={kind} disabled={starting} onChange={value => { setKind(value); resetPreview(); }} options={[
              { label: "测评 Case 实例", value: "evaluation" }, { label: "机器人任务 ID", value: "robot_task" },
            ]} /></div>
            {kind === "robot_task" && <div>机器人实例<Select aria-label="修复来源实例" value={botId || undefined} disabled={starting} loading={bots.isPending}
              placeholder="选择任务所属实例" onChange={value => { setBotId(value); resetPreview(); }}
              options={(bots.data ?? []).filter(bot => bot.runtime_kind === "gateway").map(bot => ({ value: bot.instance_id, label: bot.instance_id }))} /></div>}
            <div>{kind === "evaluation" ? "Case 实例 ID" : "机器人任务 ID"}<Input aria-label={kind === "evaluation" ? "Case 实例 ID" : "修复来源 ID"} value={sourceId} disabled={starting}
              placeholder={kind === "evaluation" ? "粘贴测评结果中的 case-… 实例 ID" : "输入任务流中的 run_id"}
              onChange={value => { setSourceId(value); resetPreview(); }} onPressEnter={() => void load()} /></div>
          </div>
          {kind === "evaluation" && <Text type="secondary">在测评结果中复制失败项的「Case 实例 ID」，粘贴后加载。Harness 会自动获取这次执行的信息，每次只处理一个 Case。</Text>}
          {kind === "robot_task" && <Text type="secondary">任务将先建立本地复现测试。缺失证据或无法在隔离环境中复现时，会记录受阻原因。</Text>}
          <Button loading={loading} disabled={starting || !sourceId.trim() || (kind === "robot_task" && !botId)} onClick={() => void load()}>加载来源</Button>
          {error && <Alert type="error" content={error} />}
          {kind === "robot_task" && bots.isError && <Alert type="error" content="机器人实例列表读取失败，请刷新后重试" />}
          {preview && <>
            <Alert type={blocked ? "warning" : "info"} content={blocked ? preview.blockers.join("；") :
              kind === "evaluation" ? "已根据 Case 实例 ID 加载来源；启动前会再次验证这次执行的失败结果。" : "已加载任务证据。启动后将先核对预期行为并建立冻结复现测试。"} />
            {kind === "evaluation" && trial && <div aria-label="待修复 Case 详情" style={{ overflowWrap: "anywhere" }}>
              <div>Case 实例 ID：{trial.case_instance_id}</div><div>所属测评：{trial.evaluation_id}</div><div>Case：{trial.case_ref}</div><div>Target：{trial.target_id} · 第 {trial.attempt} 次执行</div>
              <div>执行结果：{({ failed: "失败", error: "执行错误", passed: "通过", skipped: "跳过" } as Record<string, string>)[trial.outcome] || trial.outcome}</div>
              <Text type="secondary">同一 Case / Target 的重复执行按原次数验证，其他已通过 Case 继续作为回归保护集。</Text>
            </div>}
            {!!preview.history.length && <Space direction="vertical"><Text bold>关联历史修复 {preview.history.length} 条</Text>
              {preview.history.map(item => <Button type="text" key={item.task_id} onClick={() => openTask(item.task_id)}>{repairStatusLabel(item)} · {sourceLabel(item)}</Button>)}</Space>}
            {preview.evidence && <details><summary>查看任务证据</summary><pre style={{ maxHeight: 320, overflow: "auto", whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{JSON.stringify(preview.evidence, null, 2)}</pre></details>}
            {(!blocked || kind === "robot_task") && <>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 210px), 1fr))", gap: 16 }}>
                <div>修复模型<Input aria-label="修复模型" value={model} disabled={starting} onChange={setModel} placeholder="输入已配置的 Codex 模型" /></div>
                <div>推理强度<Select aria-label="修复推理强度" value={effort} disabled={starting} onChange={setEffort} options={["low", "medium", "high", "xhigh"].map(value => ({ value, label: value }))} /></div>
                <div>最多候选次数<InputNumber aria-label="最多候选次数" min={1} precision={0} value={attempts} disabled={starting} onChange={setAttempts} style={{ width: "100%" }} /></div>
                <div>总时间预算（秒）<InputNumber aria-label="修复时间预算" min={1} precision={0} value={seconds} disabled={starting} onChange={setSeconds} style={{ width: "100%" }} /></div>
              </div>
              <Checkbox checked={reviewAndCommit} disabled={starting} onChange={setReviewAndCommit}>AI 审核通过后，收录回归测试并创建本地提交（不推送）</Checkbox>
              <Text type="secondary">从本地 HEAD 创建专属 worktree 和分支。目标和保护集通过后执行所选后续动作；审核与提交共用本次预算，合入主分支由你决定。</Text>
              <Button type="primary" loading={starting} disabled={loading || !model.trim() || (kind === "evaluation" && (!trial || !["failed", "error"].includes(trial.outcome) || blocked))} onClick={() => void start()}>
                {blocked ? "保存自检受阻记录" : "开始自检与修复"}</Button>
            </>}
          </>}
        </Space>
      </Card>
      <Card title="修复历史" extra={<Button onClick={() => void history.refetch()}>刷新历史</Button>}>
        <Space wrap style={{ marginBottom: 16 }}><Input aria-label="搜索修复历史" placeholder="搜索修复、Case 实例、测评或任务 ID" value={search} allowClear
          onChange={value => { setSearch(value); setPage(1); }} />
          <Select aria-label="修复历史状态" value={status} style={{ width: 180 }} onChange={value => { setStatus(value); setPage(1); }} options={[
            { label: "全部状态", value: "" }, ...Object.entries(REPAIR_LABELS).map(([value, label]) => ({ value, label })),
          ]} /></Space>
        {history.isError && <Alert type="error" content={String(history.error)} />}
        <Table size="small" rowKey="task_id" loading={history.isPending} data={history.data?.tasks ?? []} scroll={{ x: 900 }}
          pagination={{ current: page, pageSize: 20, total: history.data?.total ?? 0, onChange: setPage }} columns={[
            { title: "来源", width: 340, render: (_, task) => <Button type="text" onClick={() => openTask(task.task_id)} style={{ whiteSpace: "normal", height: "auto", textAlign: "left", overflowWrap: "anywhere" }}>{sourceLabel(task)}</Button> },
            { title: "状态", render: (_, task) => <Tag color={task.status === "fixed" ? "green" : "blue"}>{repairStatusLabel(task)}</Tag> },
            { title: "阶段", render: (_, task) => stageLabel(task.stage) },
            { title: "更新时间", render: (_, task) => new Date(task.updated_at * 1000).toLocaleString() },
            { title: "操作", render: (_, task) => <Button size="small" onClick={() => openTask(task.task_id)}>查看记录</Button> },
          ]} />
      </Card>
    </Space>
    <Drawer title="AI Harness 修复记录" visible={!!taskId} width="min(900px, 100vw)" footer={null} onCancel={() => openTask("")}>
      {taskId && <RepairDetail key={taskId} taskId={taskId} />}
    </Drawer>
  </PageSection>;
}
