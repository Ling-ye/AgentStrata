import { useMemo, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Alert, Button, Checkbox, Empty, Input, InputNumber, Message, Select, Space, Spin, Tag, Typography } from "@arco-design/web-react";
import { evaluationApi } from "./evaluationApi";
import { buildSuiteRequest, formatApiError, type EvaluationRecord, type EvaluationSuite, type EvaluationCaseDescriptor, type EvaluationSubject } from "./model";

import { SUBJECTS, SCORING_LABELS, SOURCE_LABELS, PURPOSE_LABELS, SCORER_ORIGINS, catalogGroups, suiteFormKey } from "./catalogModel";

const { Text, Paragraph } = Typography;

function CasePreview({ suite, caseId, botId }: { suite: EvaluationSuite; caseId: string; botId: string }) {
  const query = useQuery({ queryKey: ["benchmark-case", botId, suite.suite_id, caseId],
    queryFn: () => evaluationApi.caseDescriptor(suite.suite_id, caseId, botId) });
  if (query.isPending) return <Spin />;
  if (query.isError) return <Alert type="error" content={formatApiError(query.error)} action={<Button onClick={() => void query.refetch()}>重试</Button>} />;
  const item: EvaluationCaseDescriptor = query.data;
  return <div className="eval-benchmark-case-detail">
    <section><Text bold>被测对象可见的输入与背景</Text><pre>{item.input || "未记录"}</pre>
      {item.context && <pre>{item.context}</pre>}<Text type="secondary">背景是任务资料，不授予会话身份或工具权限。</Text></section>
    <section><Text bold>评分侧的期望与参考资料</Text><pre>{item.expected_behavior || "未记录"}</pre>
      <Paragraph>{suite.benchmark?.scorer?.name || suite.benchmark?.native_method}</Paragraph>
      <details><summary>查看参考资料与资源</summary><pre>{JSON.stringify(item.reference_material ?? {}, null, 2)}</pre></details>
      {!!Object.keys(item.scoring ?? {}).length && <details><summary>评价步骤与阈值</summary><pre>{JSON.stringify(item.scoring, null, 2)}</pre></details>}
    </section>
    <section><Text bold>工具与环境依赖</Text><Paragraph>{item.tools?.join("、") || "未声明专用工具依赖"}</Paragraph>
      <Paragraph>{item.readiness?.environment || suite.benchmark?.target_scope}</Paragraph>
      {item.readiness?.ready === false && <Alert type="warning" content={item.readiness.reason} />}
      <details><summary>来源及附件元数据</summary><pre>{JSON.stringify(item.metadata, null, 2)}</pre></details>
    </section>
  </div>;
}

export default function BenchmarkWorkbench(props: {
  botId: string; suites: EvaluationSuite[]; active: boolean; onCreated: (record: EvaluationRecord) => void;
}) {
  const [subject, setSubject] = useState<EvaluationSubject>("agent");
  return <div className="eval-benchmark-workbench">
    <Space wrap>{SUBJECTS.map(item => <Button key={item.id} aria-pressed={subject === item.id}
      type={subject === item.id ? "primary" : "secondary"} onClick={() => setSubject(item.id)}>{item.title}</Button>)}</Space>
    <Text type="secondary">{SUBJECTS.find(item => item.id === subject)?.description}</Text>
    <SubjectCatalog key={`${props.botId}:${subject}`} {...props} subject={subject} />
  </div>;
}

function SubjectCatalog({ botId, suites, active, onCreated, subject }: {
  botId: string; suites: EvaluationSuite[]; active: boolean; onCreated: (record: EvaluationRecord) => void; subject: EvaluationSubject;
}) {
  const [suiteId, setSuiteId] = useState("");
  const [capability, setCapability] = useState("");
  const groups = catalogGroups(suites, subject, capability);
  const available = groups.flatMap(group => [...group.suites, ...group.planned]);
  const suite = available.find(item => item.suite_id === suiteId) ?? groups.flatMap(group => group.suites)[0];
  const capabilities = [...new Set(suites.filter(item => item.subject_type === subject).flatMap(item => item.capability_tags ?? []))];
  const card = (item: EvaluationSuite) => <button type="button" key={item.suite_id} aria-pressed={item.suite_id === suite?.suite_id} onClick={() => setSuiteId(item.suite_id)}>
    <strong>{item.name}</strong><span>{item.capability_tags?.slice(0, 3).join(" · ") || "能力标签未记录"}</span>
    <small>{SUBJECTS.find(entry => entry.id === item.subject_type)?.title || "对象未记录"} · {item.benchmark?.target_scope || item.execution_scope || "执行范围未记录"}</small>
    <span>{item.ready ? `可运行 · ${item.runnable_case_count ?? item.case_count} / ${item.case_count} 题` : item.implemented ? "待准备" : "待接入"}</span>
    {item.implemented && !item.ready && <small>{item.runnable_case_count ?? 0} / {item.case_count} 题满足题目依赖，运行条件待准备</small>}
    {item.uses_smoke_data && <small>冒烟数据 · 非完整官方基准</small>}
  </button>;
  return <>
    <Select aria-label="测评能力筛选" value={capability} onChange={value => { setCapability(value); setSuiteId(""); }}
      options={[{ value: "", label: "全部能力" }, ...capabilities.map(value => ({ value, label: value }))]} />
    {active && <Alert type="warning" content="该机器人已有活动评测，完成或取消后才能开始新的评测。" />}
    <div className="eval-benchmark-layout">
      <nav className="eval-benchmark-catalog" aria-label="测评集">{groups.map(group => <section key={group.id} aria-label={group.title}>
        <Text bold>{group.title}</Text><div className="eval-catalog-items">{group.suites.map(card)}</div>
        {!!group.planned.length && <details><summary>待接入（{group.planned.length}）</summary><div className="eval-catalog-items">{group.planned.map(card)}</div></details>}
      </section>)}</nav>
      {suite ? <SuiteForm key={suiteFormKey(botId, suite)} botId={botId} suite={suite} active={active} onCreated={onCreated} />
        : <Empty description={botId ? "当前没有匹配的已接入测评集，可调整筛选或展开待接入目录。" : "请先选择机器人。"} />}
    </div>
  </>;
}

function SuiteForm({ botId, suite, active, onCreated }: {
  botId: string; suite: EvaluationSuite; active: boolean; onCreated: (record: EvaluationRecord) => void;
}) {
  const [search, setSearch] = useState("");
  const [category, setCategory] = useState("");
  const [testCategory, setTestCategory] = useState("");
  const [dataCategory, setDataCategory] = useState("");
  const [tool, setTool] = useState("");
  const [selected, setSelected] = useState<string[]>([]);
  const [expanded, setExpanded] = useState<string[]>([]);
  const [mode, setMode] = useState(suite.benchmark?.default_scoring_mode ?? "native");
  const [rubric, setRubric] = useState("evidence");
  const [repetitions, setRepetitions] = useState(1);
  const [budget, setBudget] = useState(1800);
  const [page, setPage] = useState(1);
  const identity = suiteFormKey(botId, suite);
  const cases = useQuery({ queryKey: ["benchmark-cases", identity], enabled: Boolean(botId && suite.implemented),
    queryFn: () => evaluationApi.cases(suite.suite_id, botId) });
  const caseList = cases.data ?? [];
  const knownIds = useMemo(() => new Set(caseList.map(c => c.case_id)), [cases.data]);
  const selectedIds = selected.filter(id => knownIds.has(id));
  const unavailableSelection = caseList.filter(c => selectedIds.includes(c.case_id) && c.readiness?.ready === false);
  const rows = caseList.filter(c => (!dataCategory || c.category === dataCategory) && (!testCategory || (c.test_category || "task") === testCategory) && (!category || c.capability_tags?.includes(category)) && (!tool || c.tools?.includes(tool)) && `${c.case_id} ${c.summary}`.toLowerCase().includes(search.toLowerCase()));
  const tools = [...new Set(caseList.flatMap(c => c.tools ?? []))].sort();
  const categories = [...new Set(caseList.flatMap(c => c.capability_tags ?? []))].sort();
  const pages = Math.max(1, Math.ceil(rows.length / 20));
  const safePage = Math.min(page, pages);
  const visibleRows = rows.slice((safePage - 1) * 20, safePage * 20);
  const benchmark = suite?.benchmark;
  const judge = benchmark?.judge;
  const supportsMode = benchmark?.scoring_modes.includes(mode) ?? mode === "native";
  const judgeRequired = mode !== "native" && (!selectedIds.length || caseList.some(c => selectedIds.includes(c.case_id) && c.quality_required !== false));
  const blocked = !botId || !suite?.ready || active || !selectedIds.length || unavailableSelection.length > 0 || !supportsMode || (judgeRequired && !judge) || cases.isFetching || cases.isError;
  const start = useMutation({ mutationFn: () => {
    if (!suite || blocked) throw new Error("请先完成选题并修正阻断项。");
    return evaluationApi.create(buildSuiteRequest({ botId, suiteId: suite.suite_id, caseIds: selectedIds,
      preset: "custom", repetitions, maxWallSeconds: budget, seed: 0,
      options: suite.track === "qq_message_flow" ? {} : { scoring_mode: mode, ...(benchmark?.rubrics.length ? { quality_rubric: rubric } : {}) },
      dryRun: false, llmJudge: false, confirmExternalWrite: false }));
  }, onSuccess: onCreated });
  const prepare = useMutation({ mutationFn: () => evaluationApi.prepareSuite(suite!.suite_id, botId),
    onSuccess: () => Message.info("已提交数据准备任务；完成后刷新目录。") });
  return <>
      <section className="eval-benchmark-questions" aria-label="测评题目">
        {start.isError && <Alert type="error" content={formatApiError(start.error)} />}
        <Space wrap><Text bold>{benchmark?.name || suite.name}</Text><Tag>{suite.version}</Tag></Space>
        <Space wrap><Tag>来源：{SOURCE_LABELS[benchmark?.source_type || ""] || "未记录"}</Tag><Tag>建议用途：{PURPOSE_LABELS[benchmark?.purpose || ""] || "未记录"}</Tag></Space>
        <Paragraph>{suite.value}</Paragraph>
        <Paragraph>执行环境与范围：{benchmark?.target_scope || suite.execution_scope || "未记录"}</Paragraph>
        <Paragraph>覆盖范围：{benchmark?.coverage || "未记录"}</Paragraph>
        <Paragraph>数据版本 {benchmark?.data_version || suite.version} · {benchmark?.split || "划分以当前数据为准"}</Paragraph>
        <details><summary>执行与评分实现</summary><Paragraph>指标框架：{benchmark?.framework || "未记录"} {benchmark?.framework_version}</Paragraph><Paragraph>执行适配器：{benchmark?.executor?.id || suite.plugin_id} · {benchmark?.executor?.driver || suite.driver_id}</Paragraph>
        <Paragraph>默认评分：{benchmark?.scorer?.name} · {SCORER_ORIGINS[benchmark?.scorer?.origin || ""] || "来源未记录"} · {benchmark?.scorer?.version}</Paragraph></details>
        <div className="eval-benchmark-source">数据来源：{(suite.data_source === "project_files" ? "项目固定题目文件" : suite.data_source) || "未载入"} · 当前目录 {caseList.length} 题{suite.uses_smoke_data && " · 非完整官方基准"}
          {suite.official_url && <a href={suite.official_url} target="_blank" rel="noreferrer">官方说明</a>}</div>
        {!suite.ready && <Alert type="warning" content={suite.unavailable_reason || suite.setup_hint} />}
        {suite.prepare_available && <Space wrap><Button loading={prepare.isPending} onClick={() => prepare.mutate()}>准备官方数据</Button><Text type="secondary">单独执行下载；查看题目不会自动准备数据。</Text></Space>}
        {prepare.isError && <Alert type="error" content={formatApiError(prepare.error)} />}
        {cases.isError && <Alert type="error" content={formatApiError(cases.error)} action={<Button onClick={() => void cases.refetch()}>重试</Button>} />}
        <div className="eval-benchmark-filters"><Input aria-label="搜索测评题目" placeholder="搜索题目或 ID" value={search} onChange={value => { setSearch(value); setPage(1); }} allowClear />
          {caseList.some(c => c.test_category === "red_team") && <Select aria-label="测试分类" value={testCategory} onChange={value => { setTestCategory(value); setPage(1); }} options={[{ value: "", label: "全部测试分类" }, { value: "task", label: "任务能力测试" }, { value: "red_team", label: "红队测试" }]} />}
          <Select aria-label="题目能力筛选" value={category} onChange={value => { setCategory(value); setPage(1); }} options={[{ label: "全部题目能力", value: "" }, ...categories.map(value => ({ label: value, value }))]} />
          <Select aria-label="数据类别筛选" value={dataCategory} onChange={value => { setDataCategory(value); setPage(1); }} options={[{ label: "全部数据类别", value: "" }, ...[...new Set(caseList.map(c => c.category))].sort().map(value => ({ label: value, value }))]} />
          <Select aria-label="业务工具筛选" value={tool} onChange={value => { setTool(value); setPage(1); }} options={[{ label: "全部业务工具", value: "" }, ...tools.map(value => ({ label: value, value }))]} /></div>
        <Space wrap>{suite.presets?.map(p => <Button key={p.preset_id} size="small" disabled={cases.isFetching} onClick={() => { setSelected(p.case_ids.filter(id => knownIds.has(id))); setTestCategory(p.preset_id === "red-team" ? "red_team" : ""); setPage(1); }}>{({ "balanced-100": "固定 100 题子集", quick: "快速题单", full: "完整离线题单", security: "安全题单", "red-team": "红队专项", live: "联网专项", skills: "Skill 专项" } as Record<string, string>)[p.preset_id] || p.preset_id}</Button>)}
          <Button size="small" onClick={() => setSelected([...new Set([...selectedIds, ...rows.map(c => c.case_id)])])}>选择筛选结果</Button><Button size="small" onClick={() => setSelected([])}>清空选择</Button><Text type="secondary">已选 {selectedIds.length} / {caseList.length}</Text></Space>
        {!!unavailableSelection.length && <Alert type="warning" title={`所选 ${unavailableSelection.length} 题缺少运行条件，已保留完整题单`} content={unavailableSelection.map(c => <div key={c.case_id}>{c.case_id}：{c.readiness?.reason || "请查看题目详情中的环境依赖"}</div>)} />}
        {cases.isFetching ? <Spin /> : visibleRows.length ? visibleRows.map(c => <article className="eval-benchmark-case" key={c.case_id}>
          <div className="eval-benchmark-case-heading"><Checkbox aria-label={`选择 ${c.case_id}`} checked={selectedIds.includes(c.case_id)} onChange={checked => setSelected(current => checked ? [...new Set([...current, c.case_id])] : current.filter(id => id !== c.case_id))} />
            <div><Text bold>{c.summary || c.case_id}</Text>{c.test_category === "red_team" && <div><Tag color="red">红队测试</Tag><Text type="secondary">{c.red_team_surface}</Text></div>}<div className="eval-trial-meta">{c.case_id} · {c.capability_tags?.join(" · ") || c.category}{c.readiness?.ready === false && " · 待准备"}{c.has_attachments && ` · ${c.attachment_count} 个附件`}</div></div>
            <Button type="text" size="small" aria-expanded={expanded.includes(c.case_id)} onClick={() => setExpanded(ids => ids.includes(c.case_id) ? ids.filter(id => id !== c.case_id) : [...ids, c.case_id])}>{expanded.includes(c.case_id) ? "收起" : "题目详情"}</Button></div>
          {expanded.includes(c.case_id) && <CasePreview suite={suite} caseId={c.case_id} botId={botId} />}
        </article>) : <Empty description="没有匹配的题目；请检查数据准备状态或调整筛选。" />}
        {pages > 1 && <Space><Button disabled={safePage === 1} onClick={() => setPage(safePage - 1)}>上一页</Button><Text>{safePage} / {pages}</Text><Button disabled={safePage === pages} onClick={() => setPage(safePage + 1)}>下一页</Button></Space>}
      </section>
      <aside className="eval-benchmark-config" aria-label="评分与运行计划">
        <Text bold>评分与运行</Text><div className="eval-benchmark-field"><Text type="secondary">实际被测对象</Text><Text>{benchmark?.target_scope || suite.execution_scope}</Text><Text type="secondary">所选机器人的实际模型在创建前预检解析并保存。</Text></div>
        <div className="eval-benchmark-field">评分方案<Select aria-label="评分方案" value={mode} onChange={setMode} options={(benchmark?.scoring_modes ?? ["native"]).map(value => ({ value, label: SCORING_LABELS[value] || value }))} /></div>
        <div className="eval-benchmark-field"><Text type="secondary">评分规则</Text><Text>{mode === "geval" ? "strict_mode · 通过 / 不通过 · LLM 判定" : benchmark?.native_method}</Text></div>
        {judgeRequired && <div className="eval-benchmark-field"><Text>LLM-as-a-Judge → G-Eval</Text><Text type="secondary">DeepEval GEval / 多轮按 Case 使用 ConversationalGEval</Text><Text>评分模型：{judge?.model || "未配置"}</Text>
          {!judge && <Alert type="warning" content="请配置独立 Evaluation Judge 后启动 LLM 评分。" />}
          {!!benchmark?.rubrics.length && <><Select aria-label="质量量表" value={rubric} onChange={setRubric} options={benchmark.rubrics.map(r => ({ label: r.name, value: r.id }))} /><details><summary>评分步骤与阈值</summary><pre>{JSON.stringify(benchmark.rubrics.find(r => r.id === rubric), null, 2)}</pre></details></>}
          {!benchmark?.rubrics.length && <Text type="secondary">评分步骤与阈值已版本化；在题目详情中查看。</Text>}
        </div>}
        <div className="eval-benchmark-field">每题重复次数<InputNumber aria-label="重复次数" min={1} max={10} precision={0} value={repetitions} onChange={setRepetitions} /></div>
        <div className="eval-benchmark-field">总时间预算（秒）<InputNumber aria-label="时间预算" min={1} max={21600} precision={0} value={budget} onChange={setBudget} /></div>
        <div className="eval-benchmark-total"><Text bold>{selectedIds.length} 题 × {repetitions} 次</Text><Text type="secondary">被测执行 / Judge 费用：未知</Text><Text type="secondary">题单和评分配置将在启动时冻结。</Text>
          <Button type="primary" long disabled={blocked} loading={start.isPending} onClick={() => start.mutate()}>检查并开始测试</Button></div>
      </aside>
  </>;
}
