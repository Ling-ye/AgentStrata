import { lazy, useState, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { Alert, Button, Card, Space, Spin } from "@arco-design/web-react";
import { applicationsApi, type ApplicationView } from "./api";
import { ApplicationQualityScope } from "./QualityScope";

const EvalsPage = lazy(() => import("../../pages/EvalsPage"));
export default function ProjectEvaluation({
  application,
  help,
}: {
  application: ApplicationView;
  help: ReactNode;
}) {
  const id = application.id;
  const query = useQuery({
    queryKey: ["application-evaluation-service", id],
    queryFn: ({ signal }) => applicationsApi.evaluationService(id, false, signal),
    enabled: application.source === "studio",
    retry: false,
  });
  const [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  if (application.source !== "studio") return <>{help}</>;
  if (query.isPending) return <Spin />;
  if (query.data?.ready && query.data.bot_id)
    return (
      <ApplicationQualityScope key={id} id={id}>
        <EvalsPage initialApplication={query.data.bot_id} />
      </ApplicationQualityScope>
    );
  async function connect() {
    setBusy(true);
    setError("");
    try {
      await applicationsApi.evaluationService(id, true);
      await query.refetch();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Space direction="vertical" style={{ width: "100%" }}>
      <Card title="当前应用的测评服务">
        <Space wrap>
          <Button type="primary" loading={busy} onClick={() => void connect()}>
            连接应用测评服务
          </Button>
          <span>使用当前应用已绑定的源码和独立验证环境。</span>
        </Space>
        {(error || query.error) && <Alert type="warning" content={error || String(query.error)} />}
      </Card>
      {help}
    </Space>
  );
}
