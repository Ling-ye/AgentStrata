import { Expectation, expectationText } from "./Expectation";
import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Alert, Button, Empty, Input, Select, Space, Spin, Table, Tag, Typography } from "@arco-design/web-react";
import type { EvaluationRecord, EvaluationTrial } from "./model";
import { evaluationApi, normalizeTrial } from "./evaluationApi";
import { InstanceId } from "./InstanceId";
import { durationLabel, EXCLUSION_LABELS, OUTCOME_LABELS, rateLabel, VERDICT_LABELS } from "./insightsModel";

import { asObject, asText, captureLabel, executionTurns, objectList, qualityLabel, recordedInput, trialMetrics, trialSource, instructionChecks, instructionLabel, isModelOutput, modelCalls, modelOutputSummary } from "./trialModel";

const { Text, Title } = Typography;
const errorLabel = (code?: string) => ({ result_contract_error: "结果校验异常", protocol_error: "结果传输异常", storage_error: "结果保存异常", cleanup_error: "环境清理异常", judge_error: "评分异常", execution_error: "执行异常", case_timeout: "单题超时" }[code ?? ""] ?? "测评异常");

const COLORS: Record<string, string> = { passed: "green", failed: "red", error: "orange", skipped: "gray" };

export function ResultOverview({ record }: { record: EvaluationRecord }) {
  const { insights } = record;
  const llmPrimary = asObject(asObject(record.benchmark).scoring).primary === "llm_judge";
  if (!insights.counts) return <Empty description={record.status === "running" || record.status === "queued"
    ? "评测进行中，尚未生成结果摘要。" : "该评测未记录结果摘要。"} />;
  const verdict = VERDICT_LABELS[insights.verdict];
  return <div className="eval-result-overview">
    <Space wrap>
      <Text bold>结果摘要</Text>
      {verdict && <Tag color={insights.verdict === "passed" ? "green" : insights.verdict === "failed" ? "red" : "orange"}>{verdict}</Tag>}
      {!insights.complete && <Tag color="orange">{EXCLUSION_LABELS[insights.exclusion_reason] || "部分结果"}</Tag>}
    </Space>
    <div className="eval-outcome-metrics">
      <div><span>{llmPrimary ? "LLM 判定通过率" : "原生 / 工程通过率"}</span><strong>{rateLabel(llmPrimary ? insights.quality.score : insights.pass_rate)}</strong></div>
      <div><span>{llmPrimary ? "LLM 已评分覆盖" : "独立质量分"}</span><strong>{llmPrimary ? `${insights.quality.scored} / ${insights.quality.expected}` : insights.quality.score === null ? "—" : insights.quality.score.toFixed(2)}</strong><Text type="secondary">已评分 {insights.quality.scored} / {insights.quality.expected}</Text></div>
      {Object.entries(OUTCOME_LABELS).map(([key, label]) => <div key={key} className={`eval-outcome-${key}`}>
        <span>{label}</span><strong>{insights.counts?.[key as keyof typeof insights.counts]}</strong>
      </div>)}
    </div>
    <div className="eval-outcome-bar" aria-label="测试结果分布">
      {Object.entries(insights.counts).map(([key, count]) => count > 0 && <span key={key} className={`eval-outcome-${key}`}
        style={{ flex: count }} title={`${OUTCOME_LABELS[key]} ${count}`} />)}
    </div>
    <Text type="secondary">已记录 {insights.observed ?? "—"} / 计划 {insights.planned ?? "—"} 次；{llmPrimary ? "LLM 通过率只计算已判分样本；异常与未评分单独列出。" : "通过率分母包含失败、异常和跳过。"}重复执行分别计数。</Text>
    {Object.keys(insights.capabilities).length > 0 && <div className="eval-capability-list">
      {Object.entries(insights.capabilities).map(([name, counts]) => <div key={name}>
        <Text>{name}</Text><Space size={8} wrap>{Object.entries(counts).map(([key, count]) => count > 0 && <Tag key={key} color={COLORS[key]}>{OUTCOME_LABELS[key]} {count}</Tag>)}</Space>
      </div>)}
    </div>}
  </div>;
}

function BoundedText({ value }: { value: string }) {
  const [expanded, setExpanded] = useState(false);
  const limit = 2400;
  return <div className="eval-text-block"><pre className="eval-trial-text">{expanded ? value : value.slice(0, limit)}</pre>
    <Space><Button size="mini" onClick={() => void navigator.clipboard.writeText(value)}>复制</Button>
      {value.length > limit && <Button size="mini" type="text" onClick={() => setExpanded(!expanded)}>{expanded ? "收起正文" : `展开已采集全文（${value.length} 字符）`}</Button>}</Space>
  </div>;
}

function TrialDetail({ record, preview }: { record: EvaluationRecord; preview: EvaluationTrial }) {
  const active = record.status === "running" || record.status === "queued";
  const query = useQuery({
    queryKey: ["evaluation-case-body", record.evaluation_id, preview.trial_id, preview.target_id, preview.attempt],
    queryFn: ({ signal }) => evaluationApi.caseDetail(record.evaluation_id, preview.case_ref || preview.case_id,
      { trial_id: preview.trial_id, target_id: preview.target_id, attempt: String(preview.attempt) }, signal),
    enabled: preview.body_available === true,
    retry: false,
    staleTime: active ? 1000 : Infinity,
    refetchInterval: active ? 2000 : false,
  });
  const trial = query.data?.trials.find(t => t.trial_id === preview.trial_id) ?? preview;
  const input = recordedInput(record, trial);
  const turns = executionTurns(trial);
  const judging = asObject(trial.evidence.judge_evidence);
  const ifeval = asObject(trial.evidence.ifeval_metrics);
  const model = isModelOutput(record, trial);
  const response = asObject(trial.evidence.model_response);
  const calls = model ? modelCalls(trial) : [];
  const responseText = typeof response.content === "string" ? response.content : trial.final_text;
  const status = captureLabel(asText(asObject(trial.evidence.execution).state) || trial.capture_state || "");
  return <div className="eval-trial-detail">
    {!!trial.case_instance_id && <InstanceId id={trial.case_instance_id} label="Case 实例 ID" />}
    {query.isLoading && <Spin tip="正在读取输入输出…" />}
    {query.isError && <Alert type="error" content="读取本条测试详情失败，其他测试点仍可查看。" action={<Button onClick={() => void query.refetch()}>重试</Button>} />}
    {trial.error && <Alert type="error" title={errorLabel(trial.failure?.code)} content={trial.error} />}
    {status && <Tag color="orange">{status}</Tag>}
    {!!trialSource(record, trial).label && <Space wrap><Tag>{asText(trialSource(record, trial).label)}</Tag>
      {trialSource(record, trial).kind === "ifeval_subset" && <Text type="secondary">原题 {String(trialSource(record, trial).key)} · 版本 {asText(trialSource(record, trial).revision)}</Text>}</Space>}
    <section><Text bold>{input.source}</Text>{input.text ? <BoundedText value={input.text} /> : <Text type="secondary"> 未记录</Text>}</section>
    <div className="eval-io-grid">
      <section><Expectation value={trial.expectation} /></section>
      <section aria-label="被测对象最终输出"><Text bold>被测对象最终输出</Text>
        {responseText ? <BoundedText value={responseText} /> : <div><Text type="secondary">{calls.length ? "模型返回了函数调用，无文本正文。" : modelOutputSummary(record, trial)}</Text></div>}
        {model && calls.map((call, index) => <section className="eval-conversation-turn" key={call.id || index} aria-label={`模型函数调用 ${call.name}`}>
          <Text bold>{call.name || "函数名未记录"}</Text>{call.id && <div><Text type="secondary">调用 ID：{call.id}</Text></div>}
          {call.error && <Alert type="warning" content={call.error} />}
          <BoundedText value={call.error ? call.raw : JSON.stringify(call.parsed, null, 2)} />
          <details><summary>原始参数字符串</summary><BoundedText value={call.raw} /></details>
        </section>)}
        {!!calls.length && <Text type="secondary">以上是模型提出的函数调用，本次模型直测没有执行这些工具。</Text>}
        {model && <div><Text type="secondary">模型结束原因：{asText(response.finish_reason) || "未记录"}</Text></div>}
      </section>
    </div>
    {model && <details><summary>模型请求与响应数据</summary>
      {trial.evidence.model_request ? <><Text bold>实际请求</Text><BoundedText value={JSON.stringify(trial.evidence.model_request, null, 2)} /></> : <Text type="secondary">当时未记录完整请求 messages/tools。</Text>}
      <div><Text bold>{trial.evidence.model_response ? "实际响应" : "当时保存的响应字段"}</Text><BoundedText value={JSON.stringify(trial.evidence.model_response ?? { content: trial.final_text, tool_calls: trial.evidence.tool_calls }, null, 2)} /></div>
    </details>}
    {turns.length > 1 && <section><Text bold>多轮交互</Text>{turns.map((turn, index) => <div key={`${turn.conversation_id}:${turn.turn_index}`} className="eval-conversation-turn">
      <Space><Text>第 {index + 1} 轮</Text><Text type="secondary">会话 {asText(turn.conversation_id)}</Text>{captureLabel(asText(turn.state)) && <Tag>{captureLabel(asText(turn.state))}</Tag>}</Space>
      <div className="eval-io-grid"><section><Text bold>发送</Text><BoundedText value={asText(turn.input)} /></section>
        <section><Text bold>{turn.completed === false ? "等待返回" : "收到的回答"}</Text><BoundedText value={asText(turn.final_text)} /></section></div>
    </div>)}</section>}
    {turns.some(t => objectList(t.resources).length) && <section><Text bold>输入资源</Text>{turns.flatMap((turn, index) => objectList(turn.resources).map(resource =>
      <div key={`${index}:${resource.id}`}>{asText(resource.name) || asText(resource.id)} · {asText(resource.media_type)}</div>))}</section>}
    {judging.primary === "llm_judge" && <Space wrap><Tag>LLM 判定</Tag><Text>工具证据：{({ no_calls: "已采集，没有工具调用", returned_failure: "工具返回失败", returned_success: "工具已返回" } as Record<string, string>)[asText(judging.tool_outcome)] || "未记录"}</Text><Text>{asText(judging.tool_evidence_state) === "recorded" ? "采集完整" : "必需证据未完整采集"}</Text></Space>}
    {!!trial.evidence.error_code && <Text type="secondary">异常类型：{({ judge_error: "评分异常", evidence_missing: "证据采集异常", execution_error: "运行异常" } as Record<string, string>)[asText(trial.evidence.error_code)] || asText(trial.evidence.error_code)}</Text>}
    {!!Object.keys(ifeval).length && <section aria-label="IFEval 原生指标"><Text bold>IFEval 原生指标</Text><div className="eval-outcome-metrics">
      {([['prompt_strict', '严格：整题通过'], ['prompt_loose', '宽松：整题通过'], ['instruction_strict', '严格约束通过率'], ['instruction_loose', '宽松约束通过率']] as const).map(([key, label]) =>
        <div key={key}><span>{label}</span><strong>{typeof ifeval[key] === 'boolean' ? ifeval[key] ? '通过' : '未通过' : typeof ifeval[key] === 'number' ? rateLabel(ifeval[key] as number) : '未记录'}</strong></div>)}
    </div><Text type="secondary">严格口径为主结果；宽松指标不覆盖严格失败。</Text></section>}
    <section><Text bold>判分结果</Text>{trial.score === null && <Text type="secondary"> 未评分</Text>}{trialMetrics(trial).map((metric, index) => <div className="eval-metric-row" key={`${metric.name}:${index}`}>
      <Space wrap><Text bold>{asText(metric.name)}</Text><Tag color={metric.error ? "orange" : metric.passed === true ? "green" : metric.passed === false ? "red" : "gray"}>{metric.error ? "判分异常" : metric.passed === true ? "通过" : metric.passed === false ? "未通过" : "未记录"}</Tag>
        <Text>得分 {typeof metric.score === "number" ? metric.score.toFixed(2) : "未评分"} / 阈值 {typeof metric.threshold === "number" ? metric.threshold.toFixed(2) : "—"}</Text></Space>
      <div>{asText(metric.error) || asText(metric.reason)}</div>
    </div>)}
      {!!instructionChecks(trial).length && <Table size="small" pagination={false} rowKey="id" data={instructionChecks(trial)} columns={[
        { title: "指令约束", render: (_, row) => instructionLabel(asText(row.id)) },
        { title: "参数", render: (_, row) => Object.values(asObject(row.parameters)).map(v => Array.isArray(v) ? v.join("、") : String(v)).join(" · ") || "无" },
        { title: "结果", render: (_, row) => <Tag color={row.passed ? "green" : "red"}>{row.passed ? "通过" : "未通过"}</Tag> },
      ]} />}
      {judging.quality_applicable === false && <Text type="secondary">{asText(judging.quality_reason) || "本测试点使用确定性判分。"}</Text>}
      {!trialMetrics(trial).length && <div>{objectList(judging.assertions).map((a, index) => <div key={index}>{asText(a.id)} · {a.passed ? "通过" : "未通过"}</div>)}
        {Array.isArray(trial.judge?.reasons) && <BoundedText value={trial.judge.reasons.join("\n")} />}</div>}
    </section>
    {!!judging.judge_input && <details><summary>Judge 实际收到的评分资料</summary><BoundedText value={JSON.stringify(judging.judge_input, null, 2)} /></details>}
    <Space><Text>执行耗时：{trial.execution_seconds == null ? "未记录" : durationLabel(trial.execution_seconds)}</Text><Text>评分耗时：{trial.scoring_seconds == null ? "未记录" : durationLabel(trial.scoring_seconds)}</Text><Text>总耗时：{trial.duration_seconds == null ? "未记录" : durationLabel(trial.duration_seconds)}</Text></Space>
    {trial.stop_reason && <Text type="secondary">执行结束原因：{trial.stop_reason}</Text>}
    {!model && <details open><summary>工具与执行证据</summary>{objectList(trial.evidence.tool_calls).map((tool, index) => <section className="eval-conversation-turn" key={index}>
      <Text bold>{asText(tool.name)}</Text><div className="eval-io-grid"><section><Text>参数</Text><BoundedText value={JSON.stringify(tool.arguments ?? {}, null, 2)} /></section>
        <section><Text>结果</Text><BoundedText value={JSON.stringify(tool.result ?? tool.output ?? tool, null, 2)} /></section></div>
    </section>)}<BoundedText value={JSON.stringify(trial.evidence.observation_evidence ?? [], null, 2)} /></details>}
    <details><summary>原始测试记录</summary><BoundedText value={JSON.stringify(trial, null, 2)} /></details>
  </div>;
}

export function EvaluationResults({ record }: { record: EvaluationRecord }) {
  const [outcome, setOutcome] = useState("all");
  const [search, setSearch] = useState("");
  const [expanded, setExpanded] = useState<string[]>([]);
  const rows = useMemo(() => objectList(record.result?.trials).map(normalizeTrial), [record.result]);
  const filtered = rows.filter(trial => (outcome === "all" || trial.outcome === outcome)
    && `${trial.case_instance_id} ${trial.case_id} ${trial.case_ref} ${trial.dimension} ${recordedInput(record, trial).text}`.toLowerCase().includes(search.toLowerCase()));
  const observation = asObject(record.result?.execution_observation);
  const observedTurns = objectList(asObject(observation.execution).turns);
  const showObservation = observedTurns.length > 0 && !rows.some(t => t.trial_id === observation.trial_id);
  return <Space direction="vertical" size={16} style={{ width: "100%" }}>
    <ResultOverview record={record} />
    {showObservation && <section><Text bold>尚未形成完整结果：{asText(observation.case_id)}</Text><Alert type="info" content="以下为实际执行记录，不计入完成样本或通过率。" />
      {observedTurns.map((turn, index) => <div className="eval-io-grid" key={index}><section><Text>实际输入</Text><BoundedText value={asText(turn.input)} /></section>
        <section><Text>已收到的回答</Text><BoundedText value={asText(turn.final_text)} /></section></div>)}</section>}
    {!!record.result && <section className="eval-case-results">
      <Title heading={6}>测试点结果</Title>
      <div className="eval-history-filters"><Select aria-label="测试点结果筛选" value={outcome} onChange={setOutcome} options={[
        { label: "全部结果", value: "all" }, ...Object.entries(OUTCOME_LABELS).map(([value, label]) => ({ label, value })),
      ]} /><Input aria-label="搜索测试点" placeholder="搜索测试点或输入" value={search} onChange={setSearch} allowClear /><Text type="secondary">{filtered.length} 条</Text></div>
      <Table rowKey="trial_id" size="small" data={filtered} pagination={{ pageSize: 10 }} scroll={{ x: 1100 }}
        expandedRowKeys={expanded} onExpandedRowsChange={keys => setExpanded(keys.map(String))}
        expandProps={{ icon: ({ expanded, record: trial }) => <Button size="mini" type="text" aria-expanded={expanded}
          aria-label={`${expanded ? "收起" : "展开"}测试点 ${trial.case_id}`}>{expanded ? "−" : "+"}</Button> }}
        columns={[
          { title: "测试点", width: 235, render: (_, row) => <div title={row.case_ref}>{row.case_id || row.case_ref || "标识未记录"}<small className="eval-trial-meta">{row.target_id} · 第 {row.attempt} 次</small>{!!trialSource(record, row).label && <Tag size="small">{asText(trialSource(record, row).label)}</Tag>}
            {row.case_instance_id ? <InstanceId id={row.case_instance_id} label="Case 实例 ID" /> : <Text type="secondary">Case 实例 ID 未记录</Text>}</div> },
          { title: "输入", width: 210, render: (_, row) => <span className="eval-cell-preview" title={recordedInput(record, row).source}>{recordedInput(record, row).text || "未记录"}</span> },
          { title: "预期回答／行为", width: 230, render: (_, row) => <span className="eval-cell-preview">{expectationText(row.expectation)}</span> },
          { title: "被测对象最终输出", width: 240, render: (_, row) => <span className="eval-cell-preview">{modelOutputSummary(record, row)}</span> },
          { title: "结果", width: 80, render: (_, row) => <Tag color={COLORS[row.outcome] || "gray"}>{OUTCOME_LABELS[row.outcome] || "未知"}</Tag> },
          { title: "LLM 评价", width: 90, render: (_, row) => asObject(row.evidence.judge_evidence).primary === "llm_judge" ? row.outcome === "error" ? "未判定" : row.passed ? "通过" : "未通过" : qualityLabel(row) },
          { title: "耗时", width: 85, render: (_, row) => row.duration_seconds === null ? "未记录" : durationLabel(row.duration_seconds) },
        ]}
        expandedRowRender={row => <TrialDetail record={record} preview={row} />} />
    </section>}
    {Object.keys(record.summary).length > 0 && <details className="eval-raw-summary"><summary>原始结果摘要</summary><BoundedText value={JSON.stringify(record.summary, null, 2)} /></details>}
  </Space>;

}
