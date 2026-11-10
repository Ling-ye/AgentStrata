import { useQuery } from "@tanstack/react-query";
import { Alert, Button, Card, Space, Spin, Tag, Typography } from "@arco-design/web-react";
import { applicationsApi, type ImprovementReadiness as Readiness } from "./api";
const { Text } = Typography;
export function readinessSummary(value: Readiness): string {
  if (value.scoped_prompt_supported) return "独立提示词改进的源码已准备，请使用下方专属流程；不能复用仓库级全局服务。";
  return value.source_supported
    ? "已识别应用来源，但尚未验证能否执行测评与代码改进。"
    : "此应用暂不能直接运行测评或让 Harness 修改代码。";
}
export default function ImprovementReadiness({ id }: { id: string }) {
  const query = useQuery({ queryKey: ["application-improvement-readiness", id], queryFn: ({ signal }) => applicationsApi.readiness(id, signal), retry: false });
  return <Card title="测评与代码改进：接入条件" className="application-records" extra={<Button onClick={() => void query.refetch()} loading={query.isFetching}>重新检查</Button>}>
    {query.isPending ? <Spin /> : query.isError ? <Alert type="warning" content="暂时无法核对接入条件。不会因此启用测评或代码改进，也不会展示其他应用的结果。" /> : query.data && <Space direction="vertical" size={12} style={{ width: "100%" }}>
      <Alert type="info" content={readinessSummary(query.data)} />
      <Text>配置对话与安全试聊仍按各自条件使用。这项检查只读取接入信息，不会复制代码、注册应用、启动测评或修改运行环境。</Text>
      {query.data.checks.map(check => <div key={check.code}><Tag>{({ passed: "已具备", blocked: "尚未具备", not_checked: "尚未核对" })[check.status]}</Tag> <Text>{check.message}</Text></div>)}
      {!!query.data.requirements.length && <><Text bold>要接入，还需要</Text>{query.data.requirements.map(requirement => <Text key={requirement.code}>• {requirement.message}</Text>)}</>}
      <Text type="secondary">识别到应用来源不等于执行成功。模型、预算、运行版本和实际测评结果仍需单独验证。</Text>
    </Space>}
  </Card>;
}