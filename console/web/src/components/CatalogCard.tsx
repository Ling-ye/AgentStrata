import { Card, Collapse, Space, Tag, Typography } from "@arco-design/web-react";
import { catalogSurface } from "../features/catalog/surfaces";
import type { CatalogItem } from "../types";

const { Text } = Typography;

const KIND_LABELS: Record<string, { text: string; color: string }> = {
  tool_pack: { text: "工具包", color: "arcoblue" },
  tool_feature: { text: "运行特性", color: "green" },
  mcp: { text: "MCP 服务", color: "purple" },
  subagent: { text: "子代理", color: "cyan" },
  workflow: { text: "Workflow", color: "blue" },
  prompt: { text: "提示词", color: "orange" },
  context_source: { text: "上下文源", color: "green" },
};

const SURFACE_LABELS: Record<string, { text: string; color: string }> = {
  tools: { text: "工具", color: "arcoblue" },
  prompts: { text: "提示词", color: "orange" },
  agents: { text: "Agent", color: "cyan" },
  context: { text: "上下文", color: "green" },
};

const RISK_COLORS: Record<string, string> = {
  search: "green",
  readonly: "blue",
  interactive: "orange",
  write: "red",
};

interface Props {
  item: CatalogItem;
  extra?: React.ReactNode;
}

export default function CatalogCard({ item, extra }: Props) {
  const kindInfo = KIND_LABELS[item.kind] || { text: item.kind, color: "gray" };
  const surface = catalogSurface(item);
  const surfaceInfo = SURFACE_LABELS[surface] || { text: surface, color: "gray" };
  const toolCount = item.tools?.length ?? 0;

  return (
    <Card
      className="catalog-card"
      hoverable
      title={
        <Space wrap>
          <Text bold>{item.name}</Text>
          <Tag size="small" color={surfaceInfo.color} title={surfaceInfo.text}>{surfaceInfo.text}</Tag>
          <Tag size="small" color={kindInfo.color} title={kindInfo.text}>{kindInfo.text}</Tag>
          {item.risk && <Tag size="small" color={RISK_COLORS[item.risk] || "gray"} title={item.risk}>{item.risk}</Tag>}
        </Space>
      }
      extra={extra}
    >
      <div className="catalog-card-body">
        <Text type="secondary" className="cc-text-small">{item.description}</Text>
        <div style={{ marginTop: 8 }}>
          <Space size={4} wrap>
            <Tag size="small" color="gray" title={item.category}>{item.category}</Tag>
            {item.has_tools && <Tag size="small" color="blue">T</Tag>}
            {item.has_prompts && <Tag size="small" color="green">P</Tag>}
            {toolCount > 0 && <Tag size="small" color="gray">{toolCount} 工具</Tag>}
          </Space>
        </div>

        {toolCount > 0 && (
          <Collapse bordered={false} style={{ marginTop: 8 }}>
            <Collapse.Item name="tools" header={<Text type="secondary" className="cc-text-small">展开工具列表</Text>}>
              <div className="catalog-tool-list">
                {item.tools.map((tool) => (
                  <div key={tool.name} className="catalog-tool-item">
                    <Text code className="cc-text-small">{tool.name}</Text>
                    <Text type="secondary" className="cc-text-small" style={{ marginLeft: 8 }}>{tool.summary}</Text>
                  </div>
                ))}
              </div>
            </Collapse.Item>
          </Collapse>
        )}

        {item.requires_env.length > 0 && (
          <div style={{ marginTop: 4 }}>
            <Text type="secondary" className="cc-text-small">需要 env: </Text>
            {item.requires_env.map((env) => (
              <Tag key={env} size="small" color="orangered" title={env}>{env}</Tag>
            ))}
          </div>
        )}
      </div>
    </Card>
  );
}
