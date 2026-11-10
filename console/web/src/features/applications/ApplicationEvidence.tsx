import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Alert, Button, Card, Empty, Space, Spin, Tag, Typography } from "@arco-design/web-react";
import { applicationsApi } from "./api";
import { localLink } from "./navigation";
import { repairStatusLabel } from "../harness/api";
const { Text } = Typography;
export default function ApplicationEvidence({ id, evalLink, harnessLink }: { id: string; evalLink?: string; harnessLink?: string }) {
  const [evaluationPage, setEvaluationPage] = useState(1);
  const [harnessPage, setHarnessPage] = useState(1);
  const evaluations = useQuery({ queryKey: ["application-evaluations", id, evaluationPage], queryFn: ({ signal }) => applicationsApi.evaluations(id, (evaluationPage - 1) * 20, signal), retry: false });
  const harness = useQuery({ queryKey: ["application-harness", id, harnessPage], queryFn: ({ signal }) => applicationsApi.harness(id, harnessPage, signal), retry: false });
  return <Space direction="vertical" size={16} style={{ width: "100%" }}>
    <Alert type="info" content="以下记录由服务端按当前应用筛选。高级测评与 Harness 页面仍是开发者管理入口，可切换到其他应用，不是应用权限隔离边界。" />
    <Card title="该应用的测评记录" extra={<Button onClick={() => void evaluations.refetch()}>刷新测评</Button>}>
      {evaluations.isPending ? <Spin /> : evaluations.isError ? <Alert type="error" content="无法读取当前应用的测评记录，不会回退展示全局记录。" /> : !evaluations.data?.length ? <Empty description="此页暂无测评记录" /> : <Space direction="vertical" style={{ width: "100%" }}>{evaluations.data.map(record => <div key={record.evaluation_id}><Text>{record.evaluation_id}</Text> <Tag>{record.status}</Tag><Text type="secondary">{record.created_at}</Text></div>)}</Space>}
      <Space wrap><Button disabled={evaluationPage === 1 || evaluations.isFetching} onClick={() => setEvaluationPage(page => page - 1)}>上一页</Button><Text>第 {evaluationPage} 页</Text><Button disabled={evaluations.isFetching || (evaluations.data?.length ?? 0) < 20} onClick={() => setEvaluationPage(page => page + 1)}>下一页</Button>{localLink(evalLink) && <Button href={localLink(evalLink)}>前往高级测评</Button>}</Space>
    </Card>
    <Card title="该应用的 Harness 记录" extra={<Button onClick={() => void harness.refetch()}>刷新 Harness</Button>}>
      {harness.isPending ? <Spin /> : harness.isError ? <Alert type="error" content="无法读取当前应用的 Harness 记录，不会回退展示全局记录。" /> : !harness.data?.tasks.length ? <Empty description="此页暂无 Harness 记录" /> : <Space direction="vertical" style={{ width: "100%" }}>{harness.data.tasks.map(task => <div key={task.task_id}><Text>{task.task_id}</Text> <Tag>{repairStatusLabel(task)}</Tag><Text type="secondary">{task.source.goal || task.message}</Text></div>)}</Space>}
      <Space wrap><Button disabled={harnessPage === 1 || harness.isFetching} onClick={() => setHarnessPage(page => page - 1)}>上一页</Button><Text>第 {harnessPage} 页，共 {harness.data?.total ?? 0} 条</Text><Button disabled={harness.isFetching || harnessPage * 20 >= (harness.data?.total ?? 0)} onClick={() => setHarnessPage(page => page + 1)}>下一页</Button>{localLink(harnessLink) && <Button href={localLink(harnessLink)}>前往高级 Harness</Button>}</Space>
    </Card>
  </Space>;
}