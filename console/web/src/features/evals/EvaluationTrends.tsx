import { useEffect, useMemo, useRef, useState } from "react";
import { useInfiniteQuery } from "@tanstack/react-query";
import { Alert, Button, Checkbox, Empty, Radio, Select, Space, Spin, Table, Tag, Typography } from "@arco-design/web-react";
import type { EvaluationRecord, EvaluationSuite } from "./model";
import { asObject } from "./trialModel";
import { evaluationApi } from "./evaluationApi";
import { dateLabel, evaluationSuiteId, EXCLUSION_LABELS, rateLabel, revisionLabel } from "./insightsModel";
import { buildTrendPoints, groupTrendPoints, linePaths, pointModel, pointScale, pointValue, pointDuration, durationPointLabel, durationCoverage, type SplitDimension, type TrendMetric, type TrendPoint } from "./trendModel";

const { Text } = Typography;
const COLORS = ["#165dff", "#00a870", "#d46b08", "#722ed1", "#d91ad9", "#08979c", "#cf1322"];


export default function EvaluationTrends({ initialBot, bots, suites, visible, onOpen }: {
  initialBot: string; bots: string[]; suites: EvaluationSuite[]; visible: boolean; onOpen: (record: EvaluationRecord) => void;
}) {
  const [selectedBots, setSelectedBots] = useState<string[] | null>(null);
  const [days, setDays] = useState(30);
  const [anchor, setAnchor] = useState(() => Date.now());
  const [customFrom, setCustomFrom] = useState("");
  const [customTo, setCustomTo] = useState("");
  const [suite, setSuite] = useState("");
  useEffect(() => { if (!suite && suites.length) setSuite(suites[0].suite_id); }, [suite, suites]);
  const selectedSuite = suites.find(item => item.suite_id === suite);
  const llmPrimary = selectedSuite?.purpose === "business_task";
  const durationTitle = "累计执行耗时";
  const metricOptions = [...(llmPrimary ? [] : [{ value: "pass_rate", label: "原生 / 工程通过率" }]), { value: "quality", label: llmPrimary ? "LLM 判定通过率" : "独立 LLM 质量分" }, { value: "duration", label: durationTitle }];
  const [models, setModels] = useState<string[]>([]);
  const [scales, setScales] = useState<string[]>([]);
  const [cases, setCases] = useState<string[]>([]);
  const [dimensions, setDimensions] = useState<SplitDimension[]>(["agent", "model"]);
  const [metric, setMetric] = useState<TrendMetric>("pass_rate");
  useEffect(() => { if (llmPrimary && metric === "pass_rate") setMetric("quality"); }, [llmPrimary, metric]);
  const [focusedKey, setFocusedKey] = useState("");
  const [view, setView] = useState("comparable");
  const botIds = selectedBots ?? (initialBot ? [initialBot] : []);
  const from = days === -1 ? Date.parse(customFrom) : days ? anchor - days * 86400000 : null;
  const to = days === -1 ? Date.parse(customTo) : anchor;
  const validRange = (from === null || Number.isFinite(from)) && Number.isFinite(to) && (from === null || from <= to);
  const since = from !== null && Number.isFinite(from) ? new Date(from).toISOString() : undefined;
  const until = Number.isFinite(to) ? new Date(to).toISOString() : undefined;
  const query = useInfiniteQuery({
    queryKey: ["evaluation-trends", botIds, since, until],
    initialPageParam: 0,
    queryFn: ({ pageParam, signal }) => evaluationApi.listPage({ since, until, bot_ids: botIds, offset: pageParam, limit: 51 }, signal),
    getNextPageParam: (last, _pages, offset) => last.length > 50 ? offset + 50 : undefined,
    enabled: visible && validRange,
    staleTime: 30000,
  });
  const records = useMemo(() => {
    const unique = new Map<string, EvaluationRecord>();
    for (const page of query.data?.pages ?? []) for (const record of page.slice(0, 50)) unique.set(record.evaluation_id, record);
    return [...unique.values()].filter(record => evaluationSuiteId(record) === suite);
  }, [query.data, suite]);
  const allPoints = useMemo(() => buildTrendPoints(records), [records]);
  const modelOptions = [...new Set(allPoints.map(pointModel))];
  const scaleOptions = [...new Set(allPoints.map(pointScale))];
  const caseOptions = [...new Set(allPoints.flatMap(p => p.case_ids))].sort();
  const points = useMemo(() => buildTrendPoints(records, cases).filter(p => (!models.length || models.includes(pointModel(p)))
    && (!scales.length || scales.includes(pointScale(p))) && (view === "explore" || Boolean(p.record.insights.comparison_keys?.[metric]))), [records, cases, models, scales, view, metric]);
  const lines = useMemo(() => groupTrendPoints(points, dimensions, view === "comparable" ? metric : undefined), [points, dimensions, metric, view]);
  const chartRef = useRef<HTMLDivElement>(null);
  const [chartWidth, setChartWidth] = useState(900);
  useEffect(() => {
    const node = chartRef.current;
    if (!node || !visible) return;
    const resize = new ResizeObserver(([entry]) => setChartWidth(Math.max(240, entry.contentRect.width)));
    resize.observe(node);
    return () => resize.disconnect();
  }, [visible, points.length]);
  const chartEnd = chartWidth - 25;
  const focused = points.find(point => point.key === focusedKey) ?? points[points.length - 1];
  const numeric = points.map(p => pointValue(p, metric)).filter((v): v is number => v !== null);
  const maximum = metric === "duration" ? Math.max(1, ...numeric) * 1.1 : 1;
  const start = points[0]?.timestamp ?? anchor;
  const end = points[points.length - 1]?.timestamp ?? anchor;
  const x = (point: TrendPoint) => end === start ? (55 + chartEnd) / 2 : 55 + (point.timestamp - start) / (end - start) * (chartEnd - 55);
  const y = (value: number) => 210 - value / maximum * 180;
  const labelValue = (point: TrendPoint) => metric === "duration" ? durationPointLabel(point) : metric === "quality" && !llmPrimary ? (pointValue(point, metric)?.toFixed(2) ?? "—") : rateLabel(pointValue(point, metric));
  const excluded = records.filter(record => !record.insights.trend_eligible);
  const options = (values: string[]) => values.map(value => ({ value, label: value }));
  return <div className="eval-trends">
    <section className="eval-range-controls" aria-label="趋势数据范围">
    <div className="eval-trend-controls">
      <div className="eval-trend-filter">时间范围<Select aria-label="趋势时间范围" value={days} onChange={setDays} options={[
        { value: 7, label: "最近 7 天" }, { value: 30, label: "最近 30 天" }, { value: 90, label: "最近 90 天" },
        { value: 0, label: "全部时间" }, { value: -1, label: "自定义" },
      ]} /></div>
      <div className="eval-trend-filter">机器人<Select aria-label="趋势机器人" mode="multiple" value={botIds} onChange={setSelectedBots} allowClear placeholder="全部机器人" options={options(bots)} /></div>
      <div className="eval-trend-filter">测评集<Select aria-label="趋势测评集" value={suite} onChange={setSuite} options={suites.map(item => ({ value: item.suite_id, label: item.name }))} /></div>
      <div className="eval-trend-filter">模型<Select aria-label="趋势模型" mode="multiple" value={models} onChange={setModels} options={options(modelOptions)} allowClear placeholder="全部模型" /></div>
      <div className="eval-trend-filter">测试规模<Select aria-label="趋势测试规模" mode="multiple" value={scales} onChange={setScales} options={options(scaleOptions)} allowClear placeholder="全部规模" /></div>
      <div className="eval-trend-filter">测试点<Select aria-label="趋势测试点" mode="multiple" value={cases} onChange={setCases} options={options(caseOptions)} allowClear placeholder="全部测试点" /></div>
    </div>
    {days === -1 && <Space wrap><label>开始时间<input aria-label="趋势开始时间" type="datetime-local" value={customFrom} onChange={e => setCustomFrom(e.target.value)} /></label>
      <label>结束时间<input aria-label="趋势结束时间" type="datetime-local" value={customTo} onChange={e => setCustomTo(e.target.value)} /></label></Space>}
    <div className="eval-range-actions"><Button onClick={() => { setAnchor(Date.now()); if (days === -1) void query.refetch(); }}>刷新</Button></div>
    </section>
    <Space wrap><Select aria-label="趋势比较方式" value={view} onChange={setView} style={{ width: 180 }} options={[{ value: "comparable", label: "可比趋势" }, { value: "explore", label: "探索视图" }]} />
      <Text type="secondary">{view === "comparable" ? "按精确题单、环境与评分定义分组；缺少快照的旧记录可在探索视图查看。" : "探索视图允许不同条件同屏，曲线变化不直接表示能力进步。"}</Text></Space>
    <div className="eval-split-controls" role="group" aria-label="曲线分组"><Space wrap><Text>按以下维度拆线</Text>
      {([['agent', 'Agent'], ['model', '模型'], ['scale', '测试规模']] as const).map(([key, label]) => <Checkbox key={key} checked={dimensions.includes(key)}
        onChange={checked => setDimensions(current => checked ? [...current, key] : current.filter(d => d !== key))}>{label}</Checkbox>)}
    </Space></div>
    <div className="eval-chart-header"><Text bold>评测变化</Text>
      <Radio.Group className="eval-chart-metric-controls" aria-label="趋势指标" type="button" size="small" value={metric} onChange={setMetric} options={metricOptions} />
    </div>
    {llmPrimary && <Text type="secondary">LLM 判定通过率只统计已判分样本；评分不完整的记录不连接成可比曲线。</Text>}
    {metric === "duration" && <Text type="secondary">累计所选测试点的执行时间，排除质量判分。空心点表示部分采集的耗时下限。</Text>}
    {!validRange && <Alert type="info" content="请选择有效的开始和结束时间。" />}
    {query.isError && <Alert type="error" content={String(query.error)} action={<Button onClick={() => void query.refetch()}>重试</Button>} />}
    {query.isLoading && validRange ? <Spin /> : points.length ? <>
      <Space wrap>{lines.map((line, index) => <Tag key={line.key} color={COLORS[index % COLORS.length]}>{line.label} · {line.points.length} 点</Tag>)}</Space>
      <div className="eval-trend-chart" ref={chartRef}>
        <svg viewBox={`0 0 ${chartWidth} 250`} role="group" aria-label="评测进步曲线">
          {[0, .25, .5, .75, 1].map(ratio => <g key={ratio}>
            <line x1="55" x2={chartEnd} y1={y(ratio * maximum)} y2={y(ratio * maximum)} className="eval-chart-grid" />
            <text x="45" y={y(ratio * maximum) + 4} textAnchor="end">{metric === "duration" ? `${(ratio * maximum).toFixed(maximum < 10 ? 1 : 0)}s` : metric === "quality" && !llmPrimary ? ratio.toFixed(2) : `${ratio * 100}%`}</text>
          </g>)}
          {lines.map((line, index) => <g key={line.key}>
            {linePaths(line.points, metric, x, y).map((d, pathIndex) => <path key={pathIndex} d={d} className="eval-chart-line" style={{ stroke: COLORS[index % COLORS.length] }} />)}
            {line.points.map(point => pointValue(point, metric) === null ? null : <circle key={point.key} cx={x(point)} cy={y(pointValue(point, metric)!)}
              r={point.key === focused?.key ? 7 : 5} className={`eval-chart-point${metric === "duration" && !pointDuration(point).complete ? " eval-chart-point-partial" : ""}`} style={metric === "duration" && !pointDuration(point).complete
                ? { fill: "var(--color-bg-2)", stroke: COLORS[index % COLORS.length] } : { fill: COLORS[index % COLORS.length] }} role="button" tabIndex={0}
              aria-label={`查看评测 ${point.record.evaluation_id} ${point.target_id} ${labelValue(point)}${metric === "duration" ? ` ${durationCoverage(point)}` : ""}`}
              onMouseEnter={() => setFocusedKey(point.key)} onFocus={() => setFocusedKey(point.key)} onClick={() => onOpen(point.record)}
              onKeyDown={event => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); onOpen(point.record); } }}>
              <title>{`${point.record.bot_id} · ${pointModel(point)} · ${pointScale(point)}\n${dateLabel(point.record.started_at || point.record.created_at)}\n${point.record.source_revision.commit || 'Git 未记录'}\n${labelValue(point)}${metric === "duration" ? ` ${durationCoverage(point)}` : ""}`}</title>
            </circle>)}
          </g>)}
          <text x="55" y="238">{new Date(start).toLocaleDateString("zh-CN")}</text>
          <text x={chartEnd} y="238" textAnchor="end">{new Date(end).toLocaleDateString("zh-CN")}</text>
        </svg>
        {!numeric.length && <Empty description={metric === "duration" ? "所选记录没有已采集的执行耗时；历史缺项不会从总耗时反推。" : "所选记录未采集此指标。"} />}
        {focused && <div className="eval-trend-point-detail"><Space wrap>
          <Text>{dateLabel(focused.record.started_at || focused.record.created_at)}</Text><Text>{focused.record.bot_id} / {focused.backend}</Text>
          <Text>{pointModel(focused)} · {pointScale(focused)}</Text><Tag>{revisionLabel(focused.record)}</Tag>
          {metric === "duration" && <Text>{durationTitle}：{durationPointLabel(focused)} · {durationCoverage(focused)}</Text>}
          <Text>通过 {focused.counts.passed} / {focused.observed}</Text><Text>{llmPrimary ? "LLM 判定 " : "质量 "}{llmPrimary ? rateLabel(focused.quality.score) : focused.quality.score === null ? "—" : focused.quality.score.toFixed(2)} · 已评分 {focused.quality.scored} / {focused.quality.expected}</Text>
          <Text>评分模型：{String((asObject(asObject(focused.record.benchmark).scoring).judge as Record<string, unknown> | undefined)?.model ?? (focused.scoring.judge as Record<string, unknown> | undefined)?.model ?? "未记录")}</Text>
          <Button type="text" onClick={() => onOpen(focused.record)}>查看本次评测</Button>
        </Space></div>}
      </div>
      <Text type="secondary">已加载 {points.length} 个测试点。每点为一次评测中的一个执行目标；版本和测试条件变化保留在记录中。</Text>
      <Table rowKey="key" size="small" data={[...points].reverse()} pagination={{ pageSize: 10 }} scroll={{ x: 1000 }} columns={[
        { title: "评测时间", width: 180, render: (_, p) => <Button size="small" type="text" onClick={() => onOpen(p.record)}>{dateLabel(p.record.started_at || p.record.created_at)}</Button> },
        { title: "Agent / 模型", width: 230, render: (_, p) => `${p.record.bot_id} / ${p.backend} / ${pointModel(p)}` },
        { title: "规模", width: 115, render: (_, p) => pointScale(p) },
        { title: llmPrimary ? "LLM 判定通过率" : "原生 / 工程通过率", width: 145, render: (_, p) => `${rateLabel(llmPrimary ? p.quality.score : p.pass_rate)} (${p.counts.passed}/${llmPrimary ? p.quality.scored : p.observed})` },
        { title: llmPrimary ? "LLM 评分覆盖" : "独立质量分", width: 140, render: (_, p) => llmPrimary ? `${p.quality.scored}/${p.quality.expected}` : `${p.quality.score === null ? "—" : p.quality.score.toFixed(2)} (${p.quality.scored}/${p.quality.expected})` },
        { title: "Git 版本", width: 190, render: (_, p) => <span title={p.record.source_revision.commit ?? ""}>{revisionLabel(p.record)}</span> },
        { title: durationTitle, width: 190, render: (_, p) => <span>{durationPointLabel(p)}<small className="eval-trial-meta">{durationCoverage(p)}</small></span> },
      ]} />
    </> : !query.isLoading && <Empty description="当前范围没有可绘制的完整评测。可调整筛选或继续加载记录。" />}
    {query.hasNextPage && <Button loading={query.isFetchingNextPage} onClick={() => void query.fetchNextPage()}>加载更多历史记录</Button>}
    {excluded.length > 0 && <details><summary>{excluded.length} 条记录未进入曲线</summary>{excluded.map(record => <div key={record.evaluation_id}>
      <Button type="text" onClick={() => onOpen(record)}>{dateLabel(record.created_at)}</Button><Text>{EXCLUSION_LABELS[record.insights.exclusion_reason] || "记录不完整"}</Text>
    </div>)}</details>}
  </div>;
}
