import { useLayoutEffect, useRef } from "react";
import { Alert, Button, Empty, Input, Message, Pagination, Select, Spin, Tag } from "@arco-design/web-react";
import type { GatewayOverview } from "./model";
import { runState } from "./model";
import { dateTime, duration, runDuration, type ObservationFilters } from "./workbenchModel";
import { readSessionValue, saveSessionValue, scrollBookmark } from "./taskWorkspaceState";

export default function TaskRecordList({ instanceId, selected, data, loading, error, filters, range, customStart, customEnd,
  onRange, onStart, onEnd, onFilter, onReset, onPage, onSelect, onRetry }: {
  instanceId: string; selected: string; data?: GatewayOverview; loading: boolean; error: Error | null;
  filters: ObservationFilters; range: string; customStart: string; customEnd: string;
  onRange: (value: string) => void; onStart: (value: string) => void; onEnd: (value: string) => void;
  onFilter: (value: Partial<ObservationFilters>) => void; onReset: () => void; onPage: (value: number) => void;
  onSelect: (id: string) => void; onRetry: () => void;
}) {
  const scroll = useRef<HTMLDivElement>(null);
  const storageKey = `obs:list-position:${instanceId}`;
  const saved = useRef(scrollBookmark(readSessionValue(storageKey)));
  const filterKey = JSON.stringify({ ...filters, since: range === "custom" ? filters.since : range });
  const previousFilter = useRef(filterKey);
  const remember = () => {
    const element = scroll.current;
    if (!element?.getClientRects().length) return;
    const top = element.getBoundingClientRect().top;
    const anchor = Array.from(element.querySelectorAll<HTMLElement>("[data-run-id]"))
      .find((item) => item.getBoundingClientRect().bottom > top);
    saved.current = { top: element.scrollTop, anchor: anchor?.dataset.runId,
      offset: (anchor?.getBoundingClientRect().top ?? top) - top };
    saveSessionValue(storageKey, saved.current);
  };
  const restore = () => {
    const element = scroll.current;
    if (!element?.getClientRects().length) return;
    const anchor = Array.from(element.querySelectorAll<HTMLElement>("[data-run-id]"))
      .find((item) => item.dataset.runId === saved.current.anchor);
    element.scrollTop = anchor ? element.scrollTop + anchor.getBoundingClientRect().top -
      element.getBoundingClientRect().top - saved.current.offset : saved.current.top;
  };
  useLayoutEffect(() => {
    const element = scroll.current;
    if (!element) return;
    const observer = new ResizeObserver(restore);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  useLayoutEffect(() => {
    if (previousFilter.current !== filterKey) {
      saved.current = { top: 0, offset: 0 }; previousFilter.current = filterKey;
    }
    restore();
  }, [data, filterKey]);
  const copy = (id: string) => {
    void navigator.clipboard.writeText(id).then(() => Message.success("任务 ID 已复制"), () => Message.error("复制失败，请从任务详情复制 ID"));
  };
  return <aside className="obs-task-list" aria-label="任务列表">
    <header className="obs-pane-heading"><strong>任务列表</strong><span>{loading && <Spin size={12} />} {data?.total ?? data?.runs.length ?? 0} 条</span></header>
    <div className="obs-list-filters">
      <div className="obs-list-filter-row"><Select aria-label="任务时间范围" value={range} onChange={onRange} options={[
        { value: "1", label: "最近 24 小时" }, { value: "7", label: "最近 7 天" }, { value: "30", label: "最近 30 天" }, { value: "custom", label: "自定义时间" }]} />
        <Select aria-label="任务状态" value={filters.state || ""} onChange={(state) => onFilter({ state })} options={[
          { value: "", label: "全部状态" }, ...["accepted", "running", "completed", "failed", "aborted", "recovery_required"].map((value) => ({ value, label: runState(value).label }))]} /></div>
      {range === "custom" && <div className="obs-list-custom-range"><input aria-label="开始时间" type="datetime-local" value={customStart} onChange={(e) => onStart(e.target.value)} />
        <input aria-label="结束时间" type="datetime-local" value={customEnd} onChange={(e) => onEnd(e.target.value)} />
        <Button size="small" disabled={!customStart || !customEnd || customStart > customEnd}
          onClick={() => onFilter({ since: new Date(customStart).getTime() / 1000, until: new Date(customEnd).getTime() / 1000 })}>应用时间范围</Button></div>}
      <Input aria-label="搜索任务 ID" placeholder="搜索任务 ID" allowClear value={filters.search ?? ""} onChange={(search) => onFilter({ search })} />
      <details className="obs-list-more"><summary>更多筛选</summary><div className="obs-list-extra-filters">
        <Input aria-label="配置版本筛选" placeholder="配置版本" allowClear value={filters.config_id ?? ""} onChange={(config_id) => onFilter({ config_id })} />
        <Input aria-label="模型筛选" placeholder="模型" allowClear value={filters.model ?? ""} onChange={(model) => onFilter({ model })} />
        <Input aria-label="组件筛选" placeholder="工具 / 插件 ID" allowClear value={filters.component ?? ""} onChange={(component) => onFilter({ component })} />
        <Input aria-label="错误码筛选" placeholder="错误码" allowClear value={filters.error_code ?? ""} onChange={(error_code) => onFilter({ error_code })} />
        <Select aria-label="后端筛选" value={filters.backend || ""} onChange={(backend) => onFilter({ backend })} options={[
          { value: "", label: "全部 Backend" }, ...["native", "langgraph", "codex"].map((value) => ({ value, label: value }))]} />
        <Input aria-label="最短耗时" placeholder="最短耗时（毫秒）" value={filters.min_ms == null ? "" : String(filters.min_ms)}
          onChange={(value) => { if (!value || /^\d+$/.test(value)) onFilter({ min_ms: value ? Number(value) : undefined }); }} />
      </div></details>
      <Button size="mini" type="text" onClick={onReset}>重置筛选</Button>
    </div>
    {error && <Alert type="warning" content={<><span>任务列表读取失败：{error.message}</span><Button size="mini" onClick={onRetry}>重试</Button></>} />}
    <div ref={scroll} className="obs-record-scroll" onScroll={remember}>
      <ul className="obs-records">{data?.runs.map((run) => <li key={run.run_id} data-run-id={run.run_id} className={run.run_id === selected ? "is-selected" : ""}>
        <button className="obs-record-select" aria-label={`查看任务 ${run.run_id}`} aria-current={run.run_id === selected ? "true" : undefined} onClick={() => onSelect(run.run_id)}>
          <span className="obs-record-heading"><time>{dateTime(run.started_at ?? run.created_at)}</time><Tag size="small" color={runState(run.state).color}>{runState(run.state).label}</Tag></span>
          <span className="obs-record-meta"><span>{run.model || "模型未记录"}</span><span>{duration(runDuration(run))}</span></span>
        </button>
        <button className="obs-record-id" title={run.run_id} aria-label={`复制任务 ID ${run.run_id}`} onClick={() => copy(run.run_id)}>
          <code>{run.run_id.length > 22 ? `${run.run_id.slice(0, 8)}…${run.run_id.slice(-8)}` : run.run_id}</code><span>复制</span></button>
      </li>)}</ul>
      {!data?.runs.length && !loading && !error && <Empty description="没有符合条件的任务" />}
    </div>
    {data?.source === "observation_index" && <Pagination size="small" simple current={filters.page ?? 1} total={data.total ?? 0} pageSize={50} onChange={onPage} />}
  </aside>;
}
