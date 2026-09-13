import { useState } from "react";
import { Alert, Button, Checkbox, Empty, Input, Select, Space, Spin, Tag } from "@arco-design/web-react";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { captureLabels, filterSpans, spanDepth, type TracePage, type TraceSpan } from "./model";
import "./traces.css";

async function read<T>(url: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(url, { signal, cache: "no-store" });
  const body = await response.json();
  if (!response.ok) throw new Error(typeof body.detail === "string" ? body.detail : body.detail?.message || "执行记录读取失败");
  return body as T;
}
function Value({ value }: { value: unknown }) {
  const [expanded, setExpanded] = useState(false);
  const text = typeof value === "string" ? value : JSON.stringify(value, null, 2);
  if (!text) return <p>未记录</p>;
  return <div><pre className="trace-body">{expanded ? text : text.slice(0, 8000)}</pre>
    {!expanded && text.length > 8000 && <Button size="small" onClick={() => setExpanded(true)}>展开完整正文（{text.length.toLocaleString()} 字符）</Button>}</div>;
}
function Step({ span, endpoint, spans, expired }: { span: TraceSpan; endpoint: string; spans: TraceSpan[]; expired: boolean }) {
  const [open, setOpen] = useState(false);
  const query = useQuery({ queryKey: ["local-trace-step", endpoint, span.id],
    queryFn: ({ signal }) => read<Record<string, unknown>>(`${endpoint}/steps/${encodeURIComponent(span.id)}`, signal),
    enabled: open && !expired, retry: false, staleTime: Infinity });
  return <details className="trace-step" style={{ marginInlineStart: `${spanDepth(span, spans) * 12}px` }}
    onToggle={event => setOpen(event.currentTarget.open)}>
    <summary><strong>{span.name}</strong><Tag size="small">{span.type}</Tag>
      <Tag size="small" color={["failed", "error", "incomplete"].includes(span.status) ? "orange" : "gray"}>{span.status}</Tag>
      {span.coverage && <Tag size="small">{span.coverage}</Tag>}
      <time>{new Date(span.start_time).toLocaleTimeString()}</time></summary>
    {expired ? <Alert type="warning" content="正文已过期，保留步骤摘要。" /> : query.isLoading ? <Spin /> : query.isError ?
      <Alert type="error" content={String(query.error)} action={<Button onClick={() => void query.refetch()}>重试</Button>} /> : query.data &&
      <div className="trace-content"><section><h4>输入</h4><Value value={query.data.input} /></section>
        <section><h4>输出与上下文</h4><Value value={query.data.output} /></section>
        <details><summary>采集字段</summary><Value value={query.data.metadata} /></details></div>}
  </details>;
}
export default function TracePanel({ endpoint, active = false, title = "本地执行记录" }: { endpoint: string; active?: boolean; title?: string }) {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState("");
  const [type, setType] = useState("");
  const [errors, setErrors] = useState(false);
  const query = useInfiniteQuery({ queryKey: ["local-trace", endpoint], initialPageParam: 0,
    queryFn: ({ signal, pageParam }) => read<TracePage>(`${endpoint}?after=${pageParam}`, signal),
    getNextPageParam: page => page.has_more ? page.next_cursor : undefined,
    enabled: open, retry: false, refetchInterval: query => open &&
      (active || query.state.data?.pages[0]?.capture_state === "recording") ? 2500 : false });
  const first = query.data?.pages[0];
  const spans = query.data?.pages.flatMap(page => page.spans) ?? [];
  return <details className="trace-panel" onToggle={event => setOpen(event.currentTarget.open)}>
    <summary><strong>{title}</strong>{first && <Tag>{captureLabels[first.capture_state] || first.capture_state}</Tag>}</summary>
    {open && <div className="trace-panel-content">
      {query.isLoading && <Spin tip="正在读取执行记录…" />}
      {query.isError && <Alert type="error" content={String(query.error)} action={<Button onClick={() => void query.refetch()}>重试</Button>} />}
      {first && <><p>{captureLabels[first.capture_state] || first.capture_state} · 已加载 {spans.length} / {first.span_count ?? 0} 个步骤</p>
        {!!first.capture_reasons?.length && <Alert type="warning" content={`采集原因：${first.capture_reasons.join("、")}`} />}
        <Space wrap className="trace-filters"><Input aria-label="搜索执行步骤" placeholder="搜索步骤或模型" value={text} onChange={setText} allowClear />
          <Select aria-label="步骤类型" value={type} onChange={setType} options={[
            { label: "全部类型", value: "" }, { label: "模型", value: "llm" }, { label: "工具", value: "tool" },
            { label: "Agent", value: "agent" }, { label: "日志与阶段", value: "base" }]} />
          <Checkbox checked={errors} onChange={setErrors}>只看异常</Checkbox></Space>
        {filterSpans(spans, text, type, errors).map(span => <Step key={`${endpoint}:${span.id}`} span={span} endpoint={endpoint} spans={spans} expired={first.capture_state === "expired"} />)}
        {!spans.length && <Empty description={captureLabels[first.capture_state] || "没有执行步骤"} />}
        {query.hasNextPage && <Button loading={query.isFetchingNextPage} onClick={() => void query.fetchNextPage()}>加载后续步骤</Button>}
      </>}
    </div>}
  </details>;
}
export function HarnessTraces({ taskId, active }: { taskId: string; active: boolean }) {
  const [open, setOpen] = useState(false);
  const endpoint = `/api/harness/tasks/${encodeURIComponent(taskId)}/traces`;
  const query = useQuery({ queryKey: ["harness-traces", taskId],
    queryFn: ({ signal }) => read<Array<{ trace_ref: string; source: { phase?: string; kind: string } }>>(endpoint, signal),
    enabled: open, refetchInterval: open && active ? 2500 : false, retry: false });
  return <details className="trace-panel" onToggle={event => setOpen(event.currentTarget.open)}><summary><strong>本地执行记录与冻结来源</strong></summary>
    {query.isLoading && <Spin />}{query.isError && <Alert type="error" content={String(query.error)} />}
    {query.data?.map(item => <TracePanel key={item.trace_ref} endpoint={`${endpoint}/${item.trace_ref}`} title={item.source.phase || "冻结来源"} />)}
    {query.data?.length === 0 && <Empty description="尚未生成本地执行归档" />}
  </details>;
}
