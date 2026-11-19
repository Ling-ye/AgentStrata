import { useState } from "react";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { Alert, Button, Select, Space, Spin, Tag } from "@arco-design/web-react";
import { applicationsApi } from "./api";
import { applicationHref } from "./navigation";
import { stepLabels } from "../harness/flowModel";

export default function ProjectTaskRecord({
  id,
  task,
  active,
}: {
  id: string;
  task: string;
  active: boolean;
}) {
  const [open, setOpen] = useState(false),
    [showLogs, setShowLogs] = useState(false),
    [source, setSource] = useState("");
  const flow = useQuery({
    queryKey: ["project-task-flow", id, task],
    queryFn: ({ signal }) => applicationsApi.taskFlow(id, task, signal),
    enabled: open,
    retry: false,
    refetchInterval: open && active ? 3000 : false,
  });
  const logs = useInfiniteQuery({
    queryKey: ["project-task-commands", id, task, source],
    initialPageParam: "",
    queryFn: ({ signal, pageParam }) => applicationsApi.commandPage(id, task, source, pageParam, signal),
    getNextPageParam: p => (p.has_more ? p.next_cursor : undefined),
    enabled: open && showLogs,
    retry: false,
    refetchInterval: open && showLogs && active ? 5000 : false,
  });
  const receipt = flow.data?.origin;
  return (
    <details onToggle={e => setOpen(e.currentTarget.open)}>
      <summary>任务步骤与证据</summary>
      {open &&
        (flow.isPending ? (
          <Spin />
        ) : flow.error ? (
          <Alert type="warning" content={String(flow.error)} />
        ) : (
          <Space direction="vertical">
            <p className="application-prose">{flow.data.goal}</p>
            {receipt?.id && (
              <Button
                href={
                  receipt.kind === "robot_task"
                    ? applicationHref(id, "records", { run: receipt.id })
                    : applicationHref(id, "evaluations", {
                        evaluation: receipt.evaluation_id || "",
                        caseId: receipt.id,
                      })
                }
              >
                查看原始证据
              </Button>
            )}
            <ol>
              {flow.data.steps.map(s => (
                <li key={s.id}>
                  {s.title} <Tag>{stepLabels[s.status] || s.status}</Tag>
                </li>
              ))}
            </ol>
            {flow.data.candidate_digest && <span>已验收候选 · {flow.data.candidate_digest}</span>}
            <details onToggle={e => setShowLogs(e.currentTarget.open)}>
              <summary>独立命令日志</summary>
              {showLogs && (
                <>
                  <Select
                    aria-label="命令日志来源"
                    value={source || logs.data?.pages[0]?.selected || undefined}
                    onChange={setSource}
                    options={logs.data?.pages[0]?.sources.map(s => ({ value: s.id, label: s.id }))}
                  />
                  {logs.isError ? (
                    <Alert type="warning" content={String(logs.error)} />
                  ) : logs.isPending ? (
                    <Spin />
                  ) : (
                    <ul>
                      {logs.data.pages
                        .flatMap(p => p.events)
                        .map(e => (
                          <li key={e.id}>
                            {e.program} · {e.exit_code == null ? "退出状态未记录" : "退出码 " + e.exit_code}
                          </li>
                        ))}
                    </ul>
                  )}
                  {logs.hasNextPage && (
                    <Button onClick={() => void logs.fetchNextPage()}>读取后续命令</Button>
                  )}
                </>
              )}
            </details>
          </Space>
        ))}
    </details>
  );
}
