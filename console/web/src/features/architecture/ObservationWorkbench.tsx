import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { keepPreviousData, useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { Alert, Button, Empty, Spin } from "@arco-design/web-react";
import { api } from "../../api";
import type { BotInstance } from "../../types";
import BotTaskFlowPanel from "../bots/BotTaskFlowPanel";
import RunInspector from "./RunInspector";
import TaskRecordList from "./TaskRecordList";
import { type ObservationFilters } from "./workbenchModel";
import { readSessionValue, saveSessionValue, taskWorkspaceState } from "./taskWorkspaceState";
import { useTaskReadingPosition } from "./useTaskReadingPosition";

export default function ObservationWorkbench({ bot, visible }: {
  bot: BotInstance; visible: boolean;
}) {
  const initial = useMemo(() => taskWorkspaceState(bot.instance_id,
    new URLSearchParams(window.location.hash.split("?")[1] ?? ""), readSessionValue(`obs:${bot.instance_id}`)), [bot.instance_id]);
  const [selected, setSelected] = useState(initial.selected);
  const [filters, setFilters] = useState<ObservationFilters>(initial.filters);
  const [range, setRange] = useState(initial.range);
  const [customStart, setCustomStart] = useState(initial.customStart);
  const [customEnd, setCustomEnd] = useState(initial.customEnd);
  const [wide, setWide] = useState(false);
  const [listOpen, setListOpen] = useState(false);
  const workspace = useRef<HTMLDivElement>(null);
  const taskPane = useRef<HTMLElement>(null);
  const tasksVisible = visible;
  const isGateway = bot.runtime_kind === "gateway";
  const querying = tasksVisible && isGateway;
  const flowVisible = tasksVisible && (wide || !listOpen && !!selected);
  useLayoutEffect(() => {
    if (tasksVisible && !wide && listOpen) workspace.current?.scrollIntoView({ block: "start", behavior: "instant" });
  }, [tasksVisible, wide, listOpen]);
  useEffect(() => {
    const element = workspace.current;
    if (!element) return;
    const observer = new ResizeObserver(([entry]) => { if (entry.contentRect.width > 0) setWide(entry.contentRect.width >= 960); });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  useEffect(() => {
    const navigate = () => {
      const [page, query] = window.location.hash.slice(1).split("?");
      const params = new URLSearchParams(query);
      if (page !== "bots" || params.get("instance") && params.get("instance") !== bot.instance_id) return;
      if (params.get("run")) { setSelected(params.get("run")!); setListOpen(false); }
    };
    window.addEventListener("hashchange", navigate);
    return () => window.removeEventListener("hashchange", navigate);
  }, [bot.instance_id]);
  useEffect(() => {
    if (!querying || range === "custom") return;
    const timer = window.setInterval(() => setFilters((value) => ({ ...value, since: Date.now() / 1000 - Number(range) * 86400, until: undefined })), 60_000);
    return () => window.clearInterval(timer);
  }, [querying, range]);
  const overview = useQuery({ queryKey: ["observation-history", bot.instance_id, filters],
    queryFn: ({ signal }) => api.gatewayObservation(bot.instance_id, filters, signal), placeholderData: keepPreviousData,
    enabled: querying, refetchInterval: querying ? 5000 : false });
  const detail = useQuery({ queryKey: ["observation-run", bot.instance_id, selected],
    queryFn: ({ signal }) => api.gatewayRun(bot.instance_id, selected, signal), enabled: querying && !!selected, refetchInterval: querying ? 5000 : false });
  const selectedDetail = detail.data?.run.run_id === selected ? detail.data : undefined;
  const eventPages = useInfiniteQuery({ queryKey: ["observation-events", bot.instance_id, selected], initialPageParam: 0,
    queryFn: ({ pageParam, signal }) => api.observationEvents(bot.instance_id, selected, pageParam, signal),
    getNextPageParam: (last) => last.has_more ? last.next_cursor : undefined,
    enabled: querying && !!selected && selectedDetail?.source === "observation_index", refetchInterval: querying ? 5000 : false });
  const events = useMemo(() => eventPages.data?.pages.flatMap((page) => page.observations) ?? selectedDetail?.observations ?? [], [eventPages.data, selectedDetail]);
  useEffect(() => { if (querying && !selected && overview.data?.runs.length) setSelected(overview.data.runs[0].run_id); }, [querying, selected, overview.data]);
  useEffect(() => saveSessionValue(`obs:${bot.instance_id}`, { selected, filters, range, customStart, customEnd }),
    [bot.instance_id, selected, filters, range, customStart, customEnd]);
  useEffect(() => {
    if (!querying || !selected) return;
    const params = new URLSearchParams(window.location.hash.split("?")[1] ?? "");
    params.set("instance", bot.instance_id); params.set("run", selected); params.set("tab", "tasks"); params.delete("entity");
    window.history.replaceState(null, "", "#bots?" + params);
  }, [querying, bot.instance_id, selected]);
  useTaskReadingPosition({ instanceId: bot.instance_id, runId: selected, active: flowVisible && isGateway,
    ready: !!selectedDetail && (selectedDetail.source !== "observation_index" || !!eventPages.data || !!eventPages.error),
    container: taskPane, pages: eventPages.data?.pages.length ?? 1, hasMore: !!eventPages.hasNextPage,
    fetchingMore: eventPages.isFetchingNextPage, fetchMore: eventPages.fetchNextPage });
  const changeFilters = (change: Partial<ObservationFilters>) => setFilters((previous) => ({ ...previous, page: 1, ...change }));
  const refresh = () => {
    if (!isGateway) return;
    void overview.refetch(); if (selected) void detail.refetch();
    if (selectedDetail?.source === "observation_index") void eventPages.refetch();
  };
  const outsideList = !!selected && !!overview.data && !overview.isPlaceholderData && !overview.error && !overview.data.runs.some((run) => run.run_id === selected);
  return <div ref={workspace} className="obs-workbench" hidden={!visible}>
    <div className="obs-toolbar">{tasksVisible && isGateway && !wide && selected &&
      <Button onClick={() => setListOpen((value) => !value)}>{listOpen ? "返回任务" : "任务列表"}</Button>}
      <Button loading={overview.isFetching} onClick={refresh}>刷新</Button></div>
    <div hidden={!tasksVisible}>
      {!isGateway ? <BotTaskFlowPanel bot={bot} visible={tasksVisible} /> : <div className="obs-tasks-layout" data-wide={wide}>
        <div className="obs-list-column" hidden={!wide && !listOpen && !!selected}>
          <TaskRecordList instanceId={bot.instance_id} selected={selected} data={overview.data} loading={overview.isFetching} error={overview.error}
            filters={filters} range={range} customStart={customStart} customEnd={customEnd} onStart={setCustomStart} onEnd={setCustomEnd}
            onRange={(value) => { setRange(value); if (value !== "custom") changeFilters({ since: Date.now() / 1000 - Number(value) * 86400, until: undefined }); }}
            onFilter={changeFilters} onReset={() => { setRange("1"); setFilters({ since: Date.now() / 1000 - 86400, page: 1 }); }}
            onPage={(page) => setFilters((value) => ({ ...value, page }))} onSelect={(id) => { setSelected(id); setListOpen(false); }} onRetry={() => void overview.refetch()} />
        </div>
        <main ref={taskPane} className="obs-task-pane" hidden={!wide && (listOpen || !selected)}>
          {outsideList && <p className="obs-selection-notice">当前任务不在此列表中</p>}
          {detail.error && <Alert type="warning" content={<><span>任务详情读取失败：{detail.error.message}</span><Button size="mini" onClick={() => void detail.refetch()}>重试</Button></>} />}
          {selectedDetail && selected ? <>
            {eventPages.error && <Alert type="warning" content={<><span>后续事件读取失败，当前展示已取得的记录。</span><Button size="mini" onClick={() => void eventPages.refetch()}>重试</Button></>} />}
            <RunInspector key={`${bot.instance_id}:${selected}`} instanceId={bot.instance_id} detail={selectedDetail} events={events} visible={flowVisible}
              onMore={() => void eventPages.fetchNextPage()} hasMore={!!eventPages.hasNextPage} fetchingMore={eventPages.isFetchingNextPage} />
          </> : detail.isLoading && selected ? <Spin tip="读取任务记录…" /> : !detail.error && <Empty description="选择任务查看运行过程" />}
        </main>
      </div>}
    </div>
  </div>;
}
