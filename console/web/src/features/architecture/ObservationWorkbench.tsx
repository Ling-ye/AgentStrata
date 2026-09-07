import { useEffect, useMemo, useRef, useState } from "react";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { Alert, Button, Empty, Input, Pagination, Select, Spin, Tag } from "@arco-design/web-react";
import { api } from "../../api";
import type { BotInstance } from "../../types";
import BotTaskFlowPanel from "../bots/BotTaskFlowPanel";
import ConfigurationPane from "./ConfigurationPane";
import RunInspector from "./RunInspector";
import { runState } from "./model";
import { bodyState, buildRunView, dateTime, duration, runDuration, type ObservationFilters } from "./workbenchModel";

type ObservationView = "observation" | "history" | "configuration";

function initialState(instanceId: string): { selected: string; filters: ObservationFilters; range: string; customStart: string; customEnd: string } {
  const params = new URLSearchParams(window.location.hash.split("?")[1] ?? "");
  try {
    const saved = JSON.parse(sessionStorage.getItem(`obs:${instanceId}`) || "{}");
    return { selected: params.get("run") || saved.selected || "", filters: { ...saved.filters, ...(saved.range === "custom" ? {} : { since: Date.now() / 1000 - Number(saved.range || "1") * 86400, until: undefined }) }, range: saved.range || "1", customStart: saved.customStart || "", customEnd: saved.customEnd || "" };
  } catch { return { selected: params.get("run") || "", filters: { since: Date.now() / 1000 - 86400, page: 1 }, range: "1", customStart: "", customEnd: "" }; }
}

export default function ObservationWorkbench({ bot, visible, view, onNavigate, onEdit }: {
  bot: BotInstance; visible: boolean; view: ObservationView;
  onNavigate: (view: ObservationView) => void; onEdit: () => void;
}) {
  const initial = useMemo(() => initialState(bot.instance_id), [bot.instance_id]);
  const [selected, setSelected] = useState(initial.selected);
  const [filters, setFilters] = useState<ObservationFilters>(initial.filters);
  const linkedEntity = new URLSearchParams(window.location.hash.split("?")[1] ?? "").get("entity") || "";
  const [entity, setEntity] = useState(linkedEntity);
  const readingPosition = useRef(0);
  const locating = useRef(false);
  const flowVisible = visible && view === "observation";
  const [focusRequest, setFocusRequest] = useState<{ entity: string; revision: number; runId: string }>();
  const [configReveal, setConfigReveal] = useState(0);
  const [selectedEvent, setSelectedEvent] = useState<{ runId: string; seq?: number }>();
  const eventSeq = selectedEvent?.runId === selected ? selectedEvent.seq : undefined;
  useEffect(() => setSelectedEvent(undefined), [selected]);
  const [range, setRange] = useState(initial.range);
  const [customStart, setCustomStart] = useState(initial.customStart);
  const [customEnd, setCustomEnd] = useState(initial.customEnd);
  const isGateway = bot.runtime_kind === "gateway";
  useEffect(() => {
    const navigate = () => {
      const [page, query] = window.location.hash.slice(1).split("?");
      const params = new URLSearchParams(query);
      if (page !== "bots" || params.get("instance") && params.get("instance") !== bot.instance_id) return;
      const run = params.get("run");
      if (run) setSelected(run);
      const component = params.get("entity");
      if (component) { setEntity(component); setSelectedEvent(undefined); setConfigReveal((value) => value + 1); }
    };
    window.addEventListener("hashchange", navigate);
    return () => window.removeEventListener("hashchange", navigate);
  }, [bot.instance_id]);
  useEffect(() => {
    if (!flowVisible) return;
    const frame = requestAnimationFrame(() => {
      if (!locating.current) window.scrollTo(0, readingPosition.current);
      locating.current = false;
    });
    const remember = () => { readingPosition.current = window.scrollY; };
    window.addEventListener("scroll", remember, { passive: true });
    return () => { cancelAnimationFrame(frame); window.removeEventListener("scroll", remember); };
  }, [flowVisible]);
  useEffect(() => {
    if (visible && view === "configuration") setConfigReveal((value) => value + 1);
  }, [visible, view]);
  useEffect(() => {
    if (!visible || range === "custom") return;
    const timer = window.setInterval(() => setFilters((value) => ({ ...value, since: Date.now() / 1000 - Number(range) * 86400, until: undefined })), 60_000);
    return () => window.clearInterval(timer);
  }, [visible, range]);
  const overview = useQuery({ queryKey: ["observation-history", bot.instance_id, filters],
    queryFn: () => api.gatewayObservation(bot.instance_id, filters), enabled: visible && isGateway, refetchInterval: visible ? 5000 : false });
  const detail = useQuery({ queryKey: ["observation-run", bot.instance_id, selected], queryFn: () => api.gatewayRun(bot.instance_id, selected),
    enabled: visible && isGateway && !!selected, refetchInterval: visible ? 5000 : false });
  const inspection = useQuery({ queryKey: ["inspection", bot.instance_id, selected, eventSeq], queryFn: () => api.inspection(bot.instance_id, selected || undefined, eventSeq),
    enabled: visible && view === "configuration", refetchInterval: visible && view === "configuration" ? 5000 : false });
  const eventPages = useInfiniteQuery({ queryKey: ["observation-events", bot.instance_id, selected], initialPageParam: 0,
    queryFn: ({ pageParam }) => api.observationEvents(bot.instance_id, selected, pageParam),
    getNextPageParam: (last) => last.has_more ? last.next_cursor : undefined,
    enabled: visible && !!selected && detail.data?.source === "observation_index", refetchInterval: visible ? 5000 : false });
  const events = useMemo(() => eventPages.data?.pages.flatMap((page) => page.observations) ?? detail.data?.observations ?? [], [eventPages.data, detail.data]);
  const callableEntities = useMemo(() => new Set(buildRunView(events).steps.flatMap((step) =>
    [step.event.entity_id, ...(step.event.refs ?? []), step.start?.entity_id, ...(step.start?.refs ?? [])].filter((id): id is string => !!id))), [events]);
  useEffect(() => { if (!selected && overview.data?.runs.length) setSelected(overview.data.runs[0].run_id); }, [selected, overview.data]);
  useEffect(() => { try { sessionStorage.setItem(`obs:${bot.instance_id}`, JSON.stringify({ selected, filters, range, customStart, customEnd })); } catch { /* Private browsing may disable storage. */ } }, [bot.instance_id, selected, filters, range, customStart, customEnd]);
  const changeFilters = (change: Partial<ObservationFilters>) => setFilters((previous) => ({ ...previous, page: 1, ...change }));
  const selectRun = (runId: string) => {
    setSelected(runId); readingPosition.current = 0;
    const params = new URLSearchParams({ instance: bot.instance_id, run: runId });
    window.history.replaceState(null, "", "#bots?" + params);
    onNavigate("observation");
  };
  const refresh = () => {
    if (view === "configuration") void inspection.refetch();
    if (isGateway) void overview.refetch();
    if (isGateway && selected) void detail.refetch();
    if (selected && detail.data?.source === "observation_index") void eventPages.refetch();
  };
  return <div className="obs-workbench" hidden={!visible}>
    <div className="obs-toolbar"><Button loading={view === "configuration" ? inspection.isFetching : overview.isFetching} onClick={refresh}>刷新</Button></div>
    <div hidden={view !== "observation"}>
    {!!overview.data?.audit.length && <details className="obs-instance-audit"><summary>实例准入审计 · 最近 {overview.data.audit.length} 条</summary>
      <table className="obs-table"><thead><tr><th>时间</th><th>决定</th><th>原因</th><th>策略版本</th></tr></thead><tbody>{overview.data.audit.map((item, index) => <tr key={index}><td>{dateTime(item.observed_at)}</td><td>{item.allowed ? "允许" : "拒绝"}</td><td>{item.code}</td><td>{item.policy_version}</td></tr>)}</tbody></table>
      {overview.data.audit_truncated && <p className="obs-muted">此处显示最近 100 条实例审计。</p>}</details>}
    {overview.error && <Alert type="warning" content={`任务记录读取失败：${overview.error.message}`} />}
    </div>
      <main className="obs-task-pane" hidden={isGateway ? view !== "observation" : view === "configuration"}>{!isGateway ? <BotTaskFlowPanel bot={bot} visible={visible && view !== "configuration"} /> :
        detail.error ? <Alert type="error" content={`任务详情读取失败：${detail.error.message}`} /> :
          detail.isLoading && selected ? <Spin tip="读取任务记录…" /> : detail.data && selected ? <>
            {eventPages.error && <Alert type="warning" content="后续事件读取失败，当前展示已取得的记录。" />}
            <RunInspector key={`${bot.instance_id}:${selected}`} instanceId={bot.instance_id} detail={detail.data} events={events} visible={flowVisible}
              focusRequest={focusRequest?.runId === selected ? focusRequest : undefined}
              onViewConfig={(event) => {
                readingPosition.current = window.scrollY;
                setEntity(event.entity_id ?? ""); setSelectedEvent({ runId: selected, seq: event.seq }); onNavigate("configuration");
              }}
              onMore={() => void eventPages.fetchNextPage()} hasMore={!!eventPages.hasNextPage} fetchingMore={eventPages.isFetchingNextPage} />
          </> : <Empty description="当前时间范围没有任务记录" />}</main>
    {visible && view === "configuration" && <ConfigurationPane key={`${selected}:${eventSeq ?? "run"}:${entity}`} inspection={inspection.data} loading={inspection.isLoading} error={inspection.error} runId={selected}
        selectedEntity={entity} revealVersion={configReveal} callableEntities={callableEntities}
        onLocate={(id) => { locating.current = true; setFocusRequest({ entity: id, revision: Date.now(), runId: selected }); onNavigate("observation"); }}
        onEdit={onEdit} />}
    <section className="obs-history" aria-label="任务记录" hidden={view !== "history" || !isGateway}>
      <div className="obs-range-filter"><Select aria-label="观测时间范围" value={range} onChange={(value) => {
        setRange(value); if (value !== "custom") changeFilters({ since: Date.now() / 1000 - Number(value) * 86400, until: undefined });
      }} style={{ width: 150 }} options={[{ value: "1", label: "最近 24 小时" }, { value: "7", label: "最近 7 天" }, { value: "30", label: "最近 30 天" }, { value: "custom", label: "自定义时间" }]} />
      {range === "custom" && <><input aria-label="开始时间" type="datetime-local" value={customStart} onChange={(event) => setCustomStart(event.target.value)} />
        <input aria-label="结束时间" type="datetime-local" value={customEnd} onChange={(event) => setCustomEnd(event.target.value)} />
        <Button disabled={!customStart || !customEnd || customStart > customEnd} onClick={() => changeFilters({ since: new Date(customStart).getTime() / 1000, until: new Date(customEnd).getTime() / 1000 })}>应用</Button></>}
      </div>
      <div className="obs-history-filters"><Input aria-label="搜索任务 ID" placeholder="搜索任务 ID" allowClear value={filters.search ?? ""} onChange={(search) => changeFilters({ search })} />
        <Select aria-label="任务状态" placeholder="全部状态" value={filters.state || ""} onChange={(state) => changeFilters({ state })} options={[
          { value: "", label: "全部状态" }, ...["accepted", "running", "completed", "failed", "aborted", "recovery_required"].map((value) => ({ value, label: runState(value).label }))]} />
        <Input aria-label="配置版本筛选" placeholder="配置版本" allowClear value={filters.config_id ?? ""} onChange={(config_id) => changeFilters({ config_id })} />
        <Input aria-label="模型筛选" placeholder="模型" allowClear value={filters.model ?? ""} onChange={(model) => changeFilters({ model })} />
        <Input aria-label="组件筛选" placeholder="工具 / 插件 ID" allowClear value={filters.component ?? ""} onChange={(component) => changeFilters({ component })} />
        <Input aria-label="错误码筛选" placeholder="错误码" allowClear value={filters.error_code ?? ""} onChange={(error_code) => changeFilters({ error_code })} />
        <Select aria-label="后端筛选" value={filters.backend || ""} onChange={(backend) => changeFilters({ backend })} options={[{ value: "", label: "全部 Backend" }, ...["native", "langgraph", "codex"].map((value) => ({ value, label: value }))]} />
        <Input aria-label="最短耗时" placeholder="最短耗时（毫秒）" value={filters.min_ms == null ? "" : String(filters.min_ms)} onChange={(value) => { if (!value || /^\d+$/.test(value)) changeFilters({ min_ms: value ? Number(value) : undefined }); }} />
        <Button onClick={() => { setRange("1"); setFilters({ since: Date.now() / 1000 - 86400, page: 1 }); }}>重置筛选</Button>
      </div>
      {overview.error && <Alert type="warning" content={`任务记录读取失败：${overview.error.message}`} />}
      {overview.data?.legacy && <Alert type="warning" content="实例尚未生成新的观测索引，当前仅显示已有近期记录。" />}
      {overview.isFetching && <Spin size={14} />}
      <div className="obs-table-scroll"><table className="obs-table"><thead><tr><th>任务</th><th>状态</th><th>开始时间</th><th>耗时</th><th>模型 / 配置</th><th>Token</th><th>详情</th></tr></thead>
        <tbody>{overview.data?.runs.map((run) => <tr key={run.run_id} className={run.run_id === selected ? "is-selected" : ""}>
          <td><button className="obs-link" onClick={() => selectRun(run.run_id)}>{run.run_id}</button></td>
          <td><Tag color={runState(run.state).color}>{runState(run.state).label}</Tag></td><td>{dateTime(run.started_at ?? run.created_at)}</td><td>{duration(runDuration(run))}</td>
          <td><span>{run.model ?? "未记录"}</span><small>{(run.config_revision ?? run.config_id)?.slice(0, 10) ?? "未记录版本"}</small></td><td>{run.total_tokens?.toLocaleString() ?? "未记录"}</td><td>{run.details_expired ? "已到期" : bodyState(run.capture_state ?? "not_recorded")}</td>
        </tr>)}</tbody></table></div>
      {!overview.data?.runs.length && <Empty description="没有符合筛选条件的任务" />}
      {overview.data?.source === "observation_index" && <Pagination current={filters.page ?? 1} total={overview.data.total ?? 0} pageSize={50} showTotal onChange={(page) => setFilters((value) => ({ ...value, page }))} />}
    </section>
  </div>;
}
