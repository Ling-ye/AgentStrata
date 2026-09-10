import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Alert, Button, Checkbox, Empty, Input, InputNumber, Message, Select, Space, Spin, Tag, Typography } from "@arco-design/web-react";
import { evaluationApi } from "./evaluationApi";
import { buildSuiteRequest, formatApiError, type EvaluationRecord, type EvaluationSuite, type EvaluationCaseDescriptor } from "./model";

const { Text, Paragraph } = Typography;
const ORDER = ["swe-bench-verified", "bfcl", "gaia", "agentbench-fc", "agentstrata-capabilities-v1", "ifeval", "agentstrata-qq-message-flow-v1"];
export const SCORING_LABELS: Record<string, string> = { native: "仅原生评分", native_geval: "原生评分 + GEval", geval: "自定义 GEval 质量评价" };

function CasePreview({ suite, caseId, botId }: { suite: EvaluationSuite; caseId: string; botId: string }) {
  const query = useQuery({ queryKey: ["benchmark-case", botId, suite.suite_id, caseId],
    queryFn: () => evaluationApi.caseDescriptor(suite.suite_id, caseId, botId) });
  if (query.isPending) return <Spin />;
  if (query.isError) return <Alert type="error" content={formatApiError(query.error)} action={<Button onClick={() => void query.refetch()}>重试</Button>} />;
  const item: EvaluationCaseDescriptor = query.data;
  return <div className="eval-benchmark-case-detail">
    <Text bold>题目输入</Text><pre>{item.input || "未记录"}</pre>
    {item.context && <><Text bold>任务上下文</Text><pre>{item.context}</pre></>}
    <Text bold>原生评分</Text><Paragraph>{suite.benchmark?.native_method || "Suite 原生规则"}</Paragraph>
    {item.expected_behavior && <details><summary>预期行为（评分侧）</summary><pre>{item.expected_behavior}</pre></details>}
    {!!Object.keys(item.scoring ?? {}).length && <details><summary>本题质量量表与阈值</summary><pre>{JSON.stringify(item.scoring, null, 2)}</pre></details>}
    <details><summary>题目来源、附件与类别</summary><pre>{JSON.stringify(item.metadata, null, 2)}</pre></details>
  </div>;
}

export default function BenchmarkWorkbench({ botId, suites, active, onCreated }: {
  botId: string; suites: EvaluationSuite[]; active: boolean; onCreated: (record: EvaluationRecord) => void;
}) {
  const [track, setTrack] = useState("agent");
  const [suiteId, setSuiteId] = useState("agentstrata-capabilities-v1");
  const [search, setSearch] = useState("");
  const [category, setCategory] = useState("");
  const [selected, setSelected] = useState<string[]>([]);
  const [expanded, setExpanded] = useState<string[]>([]);
  const [mode, setMode] = useState("native_geval");
  const [rubric, setRubric] = useState("evidence");
  const [repetitions, setRepetitions] = useState(1);
  const [budget, setBudget] = useState(1800);
  const [page, setPage] = useState(1);
  const available = suites.filter(s => ORDER.includes(s.suite_id) && (track === "qq" ? s.track === "qq_message_flow" : s.track !== "qq_message_flow"))
    .sort((a, b) => ORDER.indexOf(a.suite_id) - ORDER.indexOf(b.suite_id));
  const suite = available.find(s => s.suite_id === suiteId) ?? available[0];
  const identity = `${botId}:${suite?.suite_id}:${suite?.benchmark?.case_set_hash ?? ""}`;
  const cases = useQuery({ queryKey: ["benchmark-cases", identity], enabled: Boolean(botId && suite?.implemented),
    queryFn: () => evaluationApi.cases(suite!.suite_id, botId) });
  useEffect(() => {
    setSelected([]); setExpanded([]); setSearch(""); setCategory(""); setPage(1);
    setMode(suite?.benchmark?.default_scoring_mode ?? "native"); setRubric("evidence");
  }, [identity]); // Selection belongs to one immutable catalog response.
  const caseList = cases.data ?? [];
  const knownIds = useMemo(() => new Set(caseList.map(c => c.case_id)), [cases.data]);
  const selectedIds = selected.filter(id => knownIds.has(id));
  const rows = caseList.filter(c => (!category || c.category === category) && `${c.case_id} ${c.summary}`.toLowerCase().includes(search.toLowerCase()));
  const categories = [...new Set(caseList.map(c => c.category))].sort();
  const pages = Math.max(1, Math.ceil(rows.length / 20));
  const safePage = Math.min(page, pages);
  const visibleRows = rows.slice((safePage - 1) * 20, safePage * 20);
  const benchmark = suite?.benchmark;
  const judge = benchmark?.judge;
  const supportsMode = benchmark?.scoring_modes.includes(mode) ?? mode === "native";
  const judgeRequired = mode !== "native" && track !== "qq" && (!selectedIds.length || caseList.some(c => selectedIds.includes(c.case_id) && c.quality_required !== false));
  const blocked = !botId || !suite?.ready || active || !selectedIds.length || !supportsMode || (judgeRequired && !judge) || cases.isFetching || cases.isError;
  const start = useMutation({ mutationFn: () => {
    if (!suite || blocked) throw new Error("请先完成选题并修正阻断项。");
    return evaluationApi.create(buildSuiteRequest({ botId, suiteId: suite.suite_id, caseIds: selectedIds,
      preset: "custom", repetitions, maxWallSeconds: budget, seed: 0,
      options: track === "qq" ? {} : { scoring_mode: mode, ...(benchmark?.rubrics.length ? { quality_rubric: rubric } : {}) },
      dryRun: false, llmJudge: false, confirmExternalWrite: false }));
  }, onSuccess: onCreated });
  const prepare = useMutation({ mutationFn: () => evaluationApi.prepareSuite(suite!.suite_id, botId),
    onSuccess: () => Message.info("已提交数据准备任务；完成后刷新目录。") });
  const choose = (id: string) => { setSuiteId(id); start.reset(); prepare.reset(); };
  if (!suite) return <Empty description={botId ? "当前没有可用的基准目录。" : "请先选择机器人。"} />;
  return <div className="eval-benchmark-workbench">
    <Space wrap><Button type={track === "agent" ? "primary" : "secondary"} onClick={() => { setTrack("agent"); start.reset(); }}>Agent / 模型能力</Button>
      <Button type={track === "qq" ? "primary" : "secondary"} onClick={() => { setTrack("qq"); start.reset(); }}>QQ 合成链路</Button></Space>
    {active && <Alert type="warning" content="该机器人已有活动评测，完成或取消后才能开始新的评测。" />}
    {start.isError && <Alert type="error" content={formatApiError(start.error)} />}
    <div className="eval-benchmark-layout">
      <nav className="eval-benchmark-catalog" aria-label="测试基准">{available.map(s => <button type="button" key={s.suite_id} aria-pressed={s.suite_id === suite.suite_id} onClick={() => choose(s.suite_id)}>
        <strong>{s.benchmark?.name || s.name}</strong><span>{s.ready ? `${s.case_count} 道可选题` : s.implemented ? "待准备" : "待接入"}</span>
        <small>{s.uses_smoke_data ? "内置冒烟题" : s.track === "agent" ? "项目回归" : `适配器 ${s.version || "未记录"}`}</small>
      </button>)}</nav>
      <section className="eval-benchmark-questions" aria-label="基准题目">
        <Space wrap><Text bold>{benchmark?.name || suite.name}</Text><Tag>{benchmark?.framework || "框架未记录"} {benchmark?.framework_version}</Tag><Tag>{suite.version}</Tag></Space>
        <Paragraph>{suite.value}</Paragraph><Text type="secondary">{benchmark?.coverage}</Text>
        <div className="eval-benchmark-source">数据来源：{suite.data_source || "未载入"} · 当前目录 {caseList.length} 题{suite.uses_smoke_data && " · 非完整官方基准"}
          {suite.official_url && <a href={suite.official_url} target="_blank" rel="noreferrer">官方说明</a>}</div>
        {!suite.ready && <Alert type="warning" content={suite.unavailable_reason || suite.setup_hint} />}
        {suite.prepare_available && <Space wrap><Button loading={prepare.isPending} onClick={() => prepare.mutate()}>准备官方数据</Button><Text type="secondary">单独执行下载；查看题目不会自动准备数据。</Text></Space>}
        {prepare.isError && <Alert type="error" content={formatApiError(prepare.error)} />}
        {cases.isError && <Alert type="error" content={formatApiError(cases.error)} action={<Button onClick={() => void cases.refetch()}>重试</Button>} />}
        <div className="eval-benchmark-filters"><Input aria-label="搜索基准题目" placeholder="搜索题目或 ID" value={search} onChange={value => { setSearch(value); setPage(1); }} allowClear />
          <Select aria-label="题目类别" value={category} onChange={value => { setCategory(value); setPage(1); }} options={[{ label: "全部类别", value: "" }, ...categories.map(value => ({ label: value, value }))]} /></div>
        <Space wrap>{suite.presets?.map(p => <Button key={p.preset_id} size="small" disabled={cases.isFetching} onClick={() => setSelected(p.case_ids.filter(id => knownIds.has(id)))}>{({ quick: "快速题单", full: "完整题单", security: "安全题单" } as Record<string, string>)[p.preset_id] || p.preset_id}</Button>)}
          <Button size="small" onClick={() => setSelected([...new Set([...selectedIds, ...rows.map(c => c.case_id)])])}>选择筛选结果</Button><Button size="small" onClick={() => setSelected([])}>清空选择</Button><Text type="secondary">已选 {selectedIds.length} / {caseList.length}</Text></Space>
        {cases.isFetching ? <Spin /> : visibleRows.length ? visibleRows.map(c => <article className="eval-benchmark-case" key={c.case_id}>
          <div className="eval-benchmark-case-heading"><Checkbox aria-label={`选择 ${c.case_id}`} checked={selectedIds.includes(c.case_id)} onChange={checked => setSelected(current => checked ? [...new Set([...current, c.case_id])] : current.filter(id => id !== c.case_id))} />
            <div><Text bold>{c.summary || c.case_id}</Text><div className="eval-trial-meta">{c.case_id} · {c.category}{c.has_attachments && ` · ${c.attachment_count} 个附件`}</div></div>
            <Button type="text" size="small" aria-expanded={expanded.includes(c.case_id)} onClick={() => setExpanded(ids => ids.includes(c.case_id) ? ids.filter(id => id !== c.case_id) : [...ids, c.case_id])}>{expanded.includes(c.case_id) ? "收起" : "题目详情"}</Button></div>
          {expanded.includes(c.case_id) && <CasePreview suite={suite} caseId={c.case_id} botId={botId} />}
        </article>) : <Empty description="没有匹配的题目；请检查数据准备状态或调整筛选。" />}
        {pages > 1 && <Space><Button disabled={safePage === 1} onClick={() => setPage(safePage - 1)}>上一页</Button><Text>{safePage} / {pages}</Text><Button disabled={safePage === pages} onClick={() => setPage(safePage + 1)}>下一页</Button></Space>}
      </section>
      <aside className="eval-benchmark-config" aria-label="评分与运行计划">
        <Text bold>评分与运行</Text><div className="eval-benchmark-field"><Text type="secondary">实际被测对象</Text><Text>{benchmark?.target_scope || suite.execution_scope}</Text><Text type="secondary">所选机器人的实际模型在创建前预检解析并保存。</Text></div>
        <div className="eval-benchmark-field">评分方案<Select aria-label="评分方案" value={mode} onChange={setMode} options={(benchmark?.scoring_modes ?? ["native"]).map(value => ({ value, label: SCORING_LABELS[value] || value }))} /></div>
        <div className="eval-benchmark-field"><Text type="secondary">原生规则</Text><Text>{mode === "geval" ? "未启用，不生成原生基准成绩" : benchmark?.native_method}</Text></div>
        {judgeRequired && <div className="eval-benchmark-field"><Text>LLM-as-a-Judge → G-Eval</Text><Text type="secondary">DeepEval GEval / 多轮按 Case 使用 ConversationalGEval</Text><Text>评分模型：{judge?.model || "未配置"}</Text>
          {!judge && <Alert type="warning" content="请配置独立 Evaluation Judge 后启动语义评分，或选择仅原生评分。" />}
          {!!benchmark?.rubrics.length && <><Select aria-label="质量量表" value={rubric} onChange={setRubric} options={benchmark.rubrics.map(r => ({ label: r.name, value: r.id }))} /><details><summary>评分步骤与阈值</summary><pre>{JSON.stringify(benchmark.rubrics.find(r => r.id === rubric), null, 2)}</pre></details></>}
          {!benchmark?.rubrics.length && <Text type="secondary">质量是否适用、步骤与阈值由每题定义；在题目详情中查看。</Text>}
        </div>}
        <div className="eval-benchmark-field">每题重复次数<InputNumber aria-label="重复次数" min={1} max={10} precision={0} value={repetitions} onChange={setRepetitions} /></div>
        <div className="eval-benchmark-field">总时间预算（秒）<InputNumber aria-label="时间预算" min={1} max={21600} precision={0} value={budget} onChange={setBudget} /></div>
        <div className="eval-benchmark-total"><Text bold>{selectedIds.length} 题 × {repetitions} 次</Text><Text type="secondary">Agent / Judge 费用：未知</Text><Text type="secondary">题单和评分配置将在启动时冻结。</Text>
          <Button type="primary" long disabled={blocked} loading={start.isPending} onClick={() => start.mutate()}>检查并开始测试</Button></div>
      </aside>
    </div>
  </div>;
}
