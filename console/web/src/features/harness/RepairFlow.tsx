import { useEffect, useRef, useState } from "react";
import { useInfiniteQuery, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Empty, Space, Spin, Tag, Typography } from "@arco-design/web-react";
import StructuredData from "../architecture/StructuredData";
import { DetailScope } from "../architecture/observationDetailState";
import { captureLabels, type TracePage, type TraceSpan } from "../traces/model";
import { harnessApi, harnessRequest } from "./api";
import { descendants, flowChildren, flowApi, initialExpansion, stepLabels, type FlowStep, type RepairFlow as Flow } from "./flowModel";
import "./repairFlow.css";

const { Text } = Typography;
const color = (status: string) => status === "completed" ? "green" : status === "running" ? "blue" : "orange";
function ExecutionBody({ taskId, refId, span }: { taskId: string; refId: string; span: TraceSpan }) {
  const query = useQuery({ queryKey: ["repair-execution-body", taskId, refId, span.id],
    queryFn: ({ signal }) => harnessRequest<{ input?: unknown; output?: unknown }>(`/tasks/${encodeURIComponent(taskId)}/traces/${refId}/steps/${span.id}`, { signal, cache: "no-store" }), retry: false, staleTime: Infinity });
  return <section className="repair-execution-message"><Text bold>{({ coding_request: "执行输入", coding_result: "执行输出", coding_error: "执行错误", agent_message: "公开过程消息" } as Record<string, string>)[span.name]}</Text>
    {query.isPending ? <Spin /> : query.isError ? <Alert type="error" content={String(query.error)} /> :
      <StructuredData value={query.data.input ?? query.data.output} />}</section>;
}
function ExecutionArchive({ taskId, refId }: { taskId: string; refId: string }) {
  const query = useInfiniteQuery({ queryKey: ["repair-execution", taskId, refId], initialPageParam: 0,
    queryFn: ({ signal, pageParam }) => harnessRequest<TracePage>(`/tasks/${encodeURIComponent(taskId)}/traces/${refId}?after=${pageParam}`, { signal, cache: "no-store" }),
    getNextPageParam: page => page.has_more ? page.next_cursor : undefined, retry: false, staleTime: Infinity });
  const first = query.data?.pages[0];
  const spans = query.data?.pages.flatMap(p => p.spans).filter(s => ["coding_request", "coding_result", "agent_message", "coding_error"].includes(s.name)) ?? [];
  return <div>{query.isPending && <Spin />}{query.isError && <Alert type="error" content={String(query.error)} action={<Button onClick={() => void query.refetch()}>重试</Button>} />}
    {first && <Text type="secondary">{captureLabels[first.capture_state] ?? first.capture_state} · 仅包含执行器可见的输入和公开输出</Text>}
    {first?.capture_reasons?.length ? <Alert type="warning" content={first.capture_reasons.join("、")} /> : null}
    {first?.capture_state !== "expired" && spans.map(span => <DetailScope key={span.id} id={span.id}><ExecutionBody taskId={taskId} refId={refId} span={span} /></DetailScope>)}
    {query.hasNextPage && <Button loading={query.isFetchingNextPage} onClick={() => void query.fetchNextPage()}>读取后续公开消息</Button>}
  </div>;
}
function StepBody({ taskId, step, active, commands }: { taskId: string; step: FlowStep; active: boolean; commands: (id: string) => void }) {
  const [archiveOpen, setArchiveOpen] = useState(false);
  const query = useQuery({ queryKey: ["repair-step", taskId, step.id, step.status, step.finished_at],
    queryFn: ({ signal }) => flowApi.step(taskId, step.id, signal), retry: false,
    refetchInterval: active && (step.status === "running" || step.phase === "checks") ? 2000 : false });
  const progress = useQuery({ queryKey: ["harness-progress", taskId], queryFn: ({ signal }) => harnessApi.progress(taskId, signal),
    enabled: active && step.status === "running" && !!step.source_id, refetchInterval: active && step.status === "running" && !!step.source_id ? 2000 : false, retry: false });
  if (query.isPending) return <Spin tip="读取步骤详情…" />;
  if (query.isError) return <Alert type="error" content={String(query.error)} action={<Button onClick={() => void query.refetch()}>重试</Button>} />;
  const data = query.data;
  return <div className="repair-step-body">
    <div className="repair-step-columns"><section><h4>输入</h4><DetailScope id="input"><StructuredData value={data.input ?? undefined} missingLabel="此步骤的输入未记录" /></DetailScope>
      {data.input_truncated && <Alert type="warning" content="关键输入保存时已有截断，以下内容并非完整输入。" />}</section>
      <section><h4>结论</h4><p>{data.conclusion}</p><h4>依据与结果</h4><DetailScope id="result"><StructuredData value={data.result ?? data.evidence ?? undefined}
        missingLabel={data.detail_state === "pending" ? "执行中，等待结果" : "此步骤的结果正文未记录"} /></DetailScope>
        {data.context_metrics != null && <><h4>上下文指标</h4><DetailScope id="context-metrics"><StructuredData value={data.context_metrics} /></DetailScope></>}
        {data.evidence != null && data.result != null && <details><summary>结论依据快照</summary><DetailScope id="evidence"><StructuredData value={data.evidence} /></DetailScope></details>}</section></div>
    <Space wrap>{step.source_id && <Button size="small" onClick={() => commands(step.source_id!)}>查看命令日志</Button>}
      {step.attempt && step.phase === "coding" && step.status === "completed" && <a href={`/api/harness/tasks/${encodeURIComponent(taskId)}/attempts/${step.attempt}/patch`} download>下载候选补丁</a>}
      </Space>
    {step.status === "running" && step.source_id && <section aria-label="步骤公开过程消息"><h4>公开过程消息</h4>
      {progress.isError && <Alert type="warning" content="公开过程消息暂不可读取；步骤状态与输入仍保留。" />}
      {progress.data?.source?.id === step.source_id ? progress.data.events.filter(e => e.type === "agent_message").map(e =>
        <pre className="repair-live-message" key={e.id}>{e.text}</pre>) : <Text type="secondary">尚无此步骤的公开消息</Text>}
      {progress.data?.source?.id === step.source_id && progress.data.truncated && <Text type="secondary">仅展示最近公开消息，完整内容等待执行归档。</Text>}</section>}
    {(data.traces.length > 0 || step.source_id) && <details onToggle={event => setArchiveOpen(event.currentTarget.open)}><summary>完整执行正文与公开过程消息</summary>
      {archiveOpen && (data.traces.length ? data.traces.map(t => <ExecutionArchive key={t.trace_ref} taskId={taskId} refId={t.trace_ref} />) :
        <Text type="secondary">{step.status === "running" ? "完整执行正文待归档；上方关键输入已可读取。" : "未记录可关联的完整执行归档。"}</Text>)}</details>}
  </div>;
}
function FlowTree({ flow, taskId, active, commands }: { flow: Flow; taskId: string; active: boolean; commands: (id: string) => void }) {
  const [expanded, setExpanded] = useState<Record<string, boolean>>(() => initialExpansion(flow));
  useEffect(() => {
    const defaults = initialExpansion(flow);
    setExpanded(current => Object.keys(defaults).some(id => !(id in current)) ? { ...defaults, ...current } : current);
  }, [flow]);
  function toggle(id: string, open: boolean) { setExpanded(values => ({ ...values, [id]: open })); }
  function stepRow(step: FlowStep) {
    return <details key={step.id} className="repair-flow-step" open={!!expanded[step.id]} onToggle={event => toggle(step.id, event.currentTarget.open)}>
      <summary><span className="repair-step-heading"><strong>{step.title}</strong><Tag size="small" color={color(step.status)}>{stepLabels[step.status] ?? step.status}</Tag>
        {step.started_at && <time>{new Date(step.started_at * 1000).toLocaleString()}</time>}</span>
        <span className="repair-input-preview">输入：{step.input_summary}</span><span className="repair-conclusion">结论：{step.conclusion}</span></summary>
      {expanded[step.id] && <DetailScope id={step.id}><StepBody taskId={taskId} step={step} active={active} commands={commands} /></DetailScope>}
    </details>;
  }
  function groupRow(id: string): React.ReactNode {
    const group = flow.groups.find(g => g.id === id)!;
    const descendantSteps = descendants(flow, id);
    if (!descendantSteps.length) return null;
    const children = flowChildren(flow, id);
    const running = descendantSteps.some(s => s.status === "running");
    return <details key={id} className="repair-flow-group" open={!!expanded[id]} onToggle={event => toggle(id, event.currentTarget.open)}>
      <summary><strong>{group.title}</strong>{running && <Tag size="small" color="blue">执行中</Tag>}
        <Text type="secondary">{descendantSteps.length} 个步骤</Text>
        {group.parent_id && <span className="repair-group-result">{(descendantSteps.find(s => s.status === "running") ?? descendantSteps[descendantSteps.length - 1])?.conclusion}</span>}</summary>
      <div className="repair-flow-children">{children.map(c => c.kind === "group" ? groupRow(c.id) : stepRow(flow.steps.find(s => s.id === c.id)!))}</div>
    </details>;
  }
  return <>{flow.groups.filter(g => !g.parent_id).map(g => groupRow(g.id))}</>;
}
export function RepairFlow({ taskId, active, commands }: { taskId: string; active: boolean; commands: (id: string) => void }) {
  const client = useQueryClient();
  const prior = useRef(active);
  const query = useQuery({ queryKey: ["repair-flow", taskId], queryFn: ({ signal }) => flowApi.flow(taskId, signal), retry: false, refetchInterval: active ? 2000 : false });
  useEffect(() => {
    if (prior.current && !active) {
      void client.cancelQueries({ queryKey: ["repair-flow", taskId] }).then(() => client.invalidateQueries({ queryKey: ["repair-flow", taskId] }));
      void client.invalidateQueries({ queryKey: ["repair-step", taskId] });
      void client.invalidateQueries({ queryKey: ["repair-commands", taskId] });
      void client.invalidateQueries({ queryKey: ["repair-command-sources", taskId] });
    }
    prior.current = active;
  }, [active, taskId, client]);
  return <section className="repair-flow" aria-label="修复流程记录"><Space wrap><Text bold>流程记录</Text><Button size="small" loading={query.isFetching} onClick={() => void query.refetch()}>刷新流程</Button></Space>
    {query.isPending && <Spin tip="读取修复流程…" />}{query.isError && <Alert type="error" content={String(query.error)} />}
    {query.data && (query.data.steps.length ? <FlowTree flow={query.data} taskId={taskId} active={active} commands={commands} /> : <Empty description="尚未记录流程步骤" />)}</section>;
}
