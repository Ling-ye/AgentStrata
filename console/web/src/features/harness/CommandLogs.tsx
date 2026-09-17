import { useState } from "react";
import { useInfiniteQuery, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Checkbox, Empty, Select, Space, Spin, Tag, Typography } from "@arco-design/web-react";
import { commandSourceLabel, flowApi, visibleCommands } from "./flowModel";

export function CommandLogs({ taskId, active, selection, setSelection }: {
  taskId: string; active: boolean; selection: { open: boolean; source: string }; setSelection: (value: { open: boolean; source: string }) => void;
}) {
  const client = useQueryClient();
  const [failures, setFailures] = useState(false);
  const sources = useQuery({ queryKey: ["repair-command-sources", taskId], queryFn: ({ signal }) => flowApi.commands(taskId, "", "", signal),
    enabled: selection.open, refetchInterval: selection.open && active ? 2000 : false, retry: false });
  const source = selection.source || sources.data?.source?.id || "";
  const query = useInfiniteQuery({ queryKey: ["repair-commands", taskId, source], initialPageParam: "",
    queryFn: ({ signal, pageParam }) => flowApi.commands(taskId, source, pageParam, signal), enabled: selection.open && !!source,
    getNextPageParam: page => page.has_more ? page.next_cursor : undefined,
    refetchInterval: selection.open && active ? 2000 : false, retry: false });
  const events = query.data?.pages.flatMap(p => p.events) ?? [];
  return <details className="repair-command-logs" id="repair-command-logs" open={selection.open}
    onToggle={event => setSelection({ ...selection, open: event.currentTarget.open })}><summary><strong>独立命令日志</strong><Typography.Text type="secondary">命令完成后更新</Typography.Text></summary>
    {selection.open && <div><Space wrap><Select aria-label="命令日志来源" style={{ minWidth: 220, maxWidth: "100%" }} value={source} placeholder="选择执行来源"
      options={sources.data?.sources.map(s => ({ value: s.id, label: commandSourceLabel(s) + (s.current ? " · 当前" : "") }))}
      onChange={value => setSelection({ open: true, source: value })} />
      <Checkbox checked={failures} onChange={setFailures}>只看非零退出码</Checkbox>
      <Button size="small" onClick={() => { void sources.refetch(); void client.resetQueries({ queryKey: ["repair-commands", taskId, source], exact: true }); }}>重新读取</Button></Space>
      {(sources.isError || query.isError) && <Alert type="error" content={String(sources.error || query.error)} />}
      {(sources.isLoading || query.isLoading) && <Spin tip="读取命令日志…" />}
      <ol className="repair-command-list">{visibleCommands(events, failures).map(event => <li key={event.id}><Tag size="small" color={event.exit_code === 0 ? "green" : "orange"}>{event.exit_code == null ? "退出状态未记录" : `退出码 ${event.exit_code}`}</Tag>
        <pre>{event.command}</pre><details><summary>命令输出</summary><pre>{event.aggregated_output || "没有公开输出"}</pre></details>
        {event.truncated && <Typography.Text type="secondary">此条正文已截断。</Typography.Text>}</li>)}</ol>
      {!events.length && !query.isLoading && <Empty description="此来源尚无已完成命令" />}
      {!!events.length && !visibleCommands(events, failures).length && <Empty description="已加载记录中没有非零退出码" />}
      {query.data?.pages.some(p => p.truncated) && <Alert type="warning" content="部分日志超出单次读取范围或正文已截断；不能视为完整执行内容。" />}
      {query.data?.pages.some(p => p.message) && <Alert type="warning" content="部分日志格式异常，已跳过；可重新读取。" />}
      {query.hasNextPage && <Button loading={query.isFetchingNextPage} onClick={() => void query.fetchNextPage()}>加载后续命令</Button>}
    </div>}
  </details>;
}
