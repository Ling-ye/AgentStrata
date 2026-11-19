import { useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Card, Input, Space, Spin } from "@arco-design/web-react";
import { applicationsApi } from "./api";
import { applicationHref } from "./navigation";

export default function ProjectRepair({
  id,
  kind,
  sourceId,
  evaluation,
}: {
  id: string;
  kind: string;
  sourceId: string;
  evaluation: string;
}) {
  const client = useQueryClient();
  const evidence = useQuery({
    queryKey: ["project-repair-origin", id, kind, sourceId],
    queryFn: ({ signal }) => applicationsApi.repairOrigin(id, kind, sourceId, signal),
    retry: false,
  });
  const [feedback, setFeedback] = useState(""),
    [busy, setBusy] = useState(false),
    [result, setResult] = useState(""),
    [error, setError] = useState("");
  const sending = useRef(false);
  const original =
    kind === "robot_task"
      ? applicationHref(id, "records", { run: sourceId })
      : applicationHref(id, "evaluations", { evaluation, caseId: sourceId });
  async function start() {
    if (!evidence.data || sending.current) return;
    sending.current = true;
    setBusy(true);
    setError("");
    try {
      // This exact source and feedback identify the same handoff, including after an unknown response.
      const bytes = new TextEncoder().encode(
        JSON.stringify([id, kind, sourceId, evidence.data.digest, feedback.trim()]),
      );
      const key = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)).slice(0, 16), n =>
        n.toString(16).padStart(2, "0"),
      ).join("");
      const response = await applicationsApi.startProjectRepair(id, {
        id: key,
        kind,
        source_id: sourceId,
        digest: evidence.data.digest,
        feedback: feedback.trim(),
      });
      setResult(response.task_id || response.message_id || key);
      await Promise.all([
        client.invalidateQueries({ queryKey: ["application-management", id] }),
        client.invalidateQueries({ queryKey: ["application-development-tasks", id] }),
      ]);
    } catch (e) {
      setError(String(e));
    } finally {
      sending.current = false;
      setBusy(false);
    }
  }
  return (
    <Card title="核对修复来源">
      <Space direction="vertical" style={{ width: "100%" }}>
        <span>
          {kind === "evaluation" ? "失败用例" : "业务任务"} · {sourceId}
        </span>
        <Button href={original}>返回原始记录</Button>
        {evidence.isPending ? (
          <Spin />
        ) : evidence.isError ? (
          <Alert
            type="warning"
            content={String(evidence.error)}
            action={<Button onClick={() => void evidence.refetch()}>重新核对</Button>}
          />
        ) : (
          <>
            <p>来源已绑定当前源码。{evidence.data.case_ref || ""} 修复沿用本应用的可写范围和独立验收。</p>
            <Input.TextArea
              aria-label="补充修复预期"
              value={feedback}
              onChange={setFeedback}
              disabled={busy || !!result}
              placeholder="补充修复线索或说明预期行为"
            />
            <Button type="primary" disabled={!!result} loading={busy} onClick={() => void start()}>
              确认并发起应用修复
            </Button>
          </>
        )}
        {error && <Alert type="error" content={error} />}
        {result && (
          <Alert
            type="success"
            content={"已登记修复 · " + result}
            action={<Button href={applicationHref(id, "versions")}>查看本应用记录</Button>}
          />
        )}
      </Space>
    </Card>
  );
}
