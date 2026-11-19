import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Alert, Button, Card, Empty, Space, Spin, Tag } from "@arco-design/web-react";
import { useHarnessClient } from "../harness/useHarnessClient";
import { applicationHref } from "./navigation";
import { RepairDetail } from "../harness/RepairDetail";
import { deliveryLabel, sourceLabel, type RepairTask } from "../harness/api";

export default function ApplicationVersions({ id, taskId }: { id: string; taskId: string }) {
  const { harnessApi } = useHarnessClient();
  const [page, setPage] = useState(1);
  const versions = useQuery({
    queryKey: ["application-versions", id, page],
    queryFn: ({ signal }) => harnessApi.history(page, "", "fixed", signal, "repair"),
    retry: false,
  });
  function restart(task: RepairTask) {
    const kind = task.source.kind === "robot_task" ? "robot_task" : "evaluation";
    window.location.hash = applicationHref(id, "repairs", {
      sourceKind: kind,
      sourceId: task.source.run_id || task.source.case_instance_id || "",
    });
  }
  return (
    <Card title="版本与交付">
      <Space direction="vertical" size={14} style={{ width: "100%" }}>
        {versions.isPending ? (
          <Spin />
        ) : versions.isError ? (
          <Alert type="error" content={String(versions.error)} />
        ) : !versions.data.tasks.length ? (
          <Empty description="还没有已完成修复的版本" />
        ) : (
          versions.data.tasks.map(task => (
            <div key={task.task_id}>
              <Button href={applicationHref(id, "versions", { task: task.task_id })}>
                {sourceLabel(task)}
              </Button>
              <Tag>{task.delivery ? deliveryLabel(task.delivery.state) : "查看验收记录"}</Tag>
            </div>
          ))
        )}
        {versions.data && versions.data.total > 20 && (
          <Space>
            <Button disabled={page === 1} onClick={() => setPage(page - 1)}>
              上一页
            </Button>
            <Button disabled={page * 20 >= versions.data.total} onClick={() => setPage(page + 1)}>
              下一页
            </Button>
          </Space>
        )}
        {taskId && (
          <RepairDetail
            key={taskId}
            taskId={taskId}
            onRestart={restart}
            onSelect={task => {
              window.location.hash = applicationHref(id, "versions", { task });
            }}
          />
        )}
      </Space>
    </Card>
  );
}
