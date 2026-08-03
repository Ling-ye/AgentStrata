import { Collapse, Descriptions, Space, Tag, Typography } from "@arco-design/web-react";
import type { BotInventory, FileEntry, McpBinding, SubagentInfo, ToolPackDetail } from "../types";
import { healthTagColor, infraStateLabel } from "../shared/ui/status";

const { Text } = Typography;

interface Props {
  data: BotInventory;
}

// ---------------------------------------------------------------------------
// Risk badge
// ---------------------------------------------------------------------------

export function riskColor(risk: string): "orange" | "green" | "red" | "blue" {
  if (risk === "search") return "orange";
  if (risk === "readonly") return "green";
  if (risk === "write") return "red";
  return "blue";
}

export function RiskBadge({ risk }: { risk: string }) {
  if (!risk) return null;
  return <Tag size="small" color={riskColor(risk)} title={risk}>{risk}</Tag>;
}

// ---------------------------------------------------------------------------
// A. MCP Services
// ---------------------------------------------------------------------------

function McpSection({ items }: { items: McpBinding[] }) {
  if (items.length === 0) {
    return <Text type="secondary" className="cc-text-small">未绑定外部 MCP 服务</Text>;
  }
  return (
    <div className="inv-mcp-list">
      {items.map((mcp) => (
        <div key={mcp.ref} className="inv-mcp-item">
          <div className="inv-mcp-header">
            <Text bold>{mcp.title}</Text>
            <Space size={4} wrap>
              {!mcp.enabled && <Tag size="small" color="gray">已禁用</Tag>}
              <RiskBadge risk={mcp.risk} />
              {mcp.infra_state && (
                <Tag size="small" className="cc-status-tag" color={healthTagColor(mcp.infra_color)}>
                  {infraStateLabel(mcp.infra_state)}
                </Tag>
              )}
            </Space>
          </div>
          <div className="inv-mcp-meta">
            <Tag size="small" className="cc-tag-meta" title={`ref: ${mcp.ref}`}>ref: {mcp.ref}</Tag>
            {mcp.exposure && <Tag size="small" color="blue" title={mcp.exposure}>{mcp.exposure}</Tag>}
            {mcp.allowed_subagents.map((sa) => (
              <Tag key={sa} size="small" color="cyan" title={sa}>{sa}</Tag>
            ))}
            {mcp.transport && <Tag size="small" className="cc-tag-meta" title={mcp.transport}>{mcp.transport}</Tag>}
          </div>
        </div>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// B. Tool packs and features
// ---------------------------------------------------------------------------

function groupByNamespace(caps: ToolPackDetail[]): { ns: string; label: string; items: ToolPackDetail[] }[] {
  const map = new Map<string, { ns: string; label: string; items: ToolPackDetail[] }>();
  for (const cap of caps) {
    let group = map.get(cap.namespace);
    if (!group) {
      group = { ns: cap.namespace, label: cap.label, items: [] };
      map.set(cap.namespace, group);
    }
    group.items.push(cap);
  }
  return Array.from(map.values());
}

function packShortName(id: string): string {
  const dot = id.indexOf(".");
  return dot >= 0 ? id.slice(dot + 1) : id;
}

function ToolPackSection({ items, hidden }: { items: ToolPackDetail[]; hidden: string[] }) {
  if (items.length === 0) {
    return <Text type="secondary" className="cc-text-small">未启用工具包</Text>;
  }
  const groups = groupByNamespace(items);
  return (
    <div className="inv-cap-groups">
      {groups.map((group) => (
        <div key={group.ns} className="inv-cap-group">
          <Text bold className="cc-text-small inv-cap-ns-label">{group.label}</Text>
          <div className="inv-cap-tags">
            {group.items.map((cap) => (
              <span key={cap.id} title={cap.description || cap.id}>
                <Tag
                  size="small"
                  color="light-blue"
                  className="inv-cap-tag"
                >
                  {packShortName(cap.id)}
                  {cap.has_tools && <span className="inv-cap-icon" title="提供工具"> T</span>}
                  {cap.has_prompts && <span className="inv-cap-icon" title="提供 prompt"> P</span>}
                </Tag>
              </span>
            ))}
          </div>
        </div>
      ))}
      {hidden.length > 0 && (
        <div className="inv-cap-excluded">
          <Text type="secondary" className="cc-text-small">已隐藏工具：</Text>
          <Space size={4} wrap>
            {hidden.map((t) => <Tag key={t} size="small" color="red" title={t}>{t}</Tag>)}
          </Space>
        </div>
      )}
    </div>
  );
}

function ToolFeatureSection({ items }: { items: ToolPackDetail[] }) {
  if (items.length === 0) {
    return <Text type="secondary" className="cc-text-small">未启用工具特性</Text>;
  }
  return (
    <Space size={4} wrap>
      {items.map((item) => (
        <Tag key={item.id} size="small" color="green" title={item.description || item.id}>
          {item.id}
        </Tag>
      ))}
    </Space>
  );
}

// ---------------------------------------------------------------------------
// C. Subagents & Workflows
// ---------------------------------------------------------------------------

function SubagentSection({ subagents, workflows }: { subagents: SubagentInfo[]; workflows: string[] }) {
  if (subagents.length === 0 && workflows.length === 0) {
    return <Text type="secondary" className="cc-text-small">未启用 Subagent</Text>;
  }
  return (
    <div className="inv-sa-list">
      {subagents.map((sa) => (
        <div key={sa.name} className="inv-sa-item">
          <div className="inv-sa-header">
            <Space size={4} wrap>
              <Text bold>{sa.name}</Text>
              {sa.kind && <Tag size="small" color="blue" title={sa.kind}>{sa.kind}</Tag>}
              {sa.tool_name && <Tag size="small" className="cc-tag-meta" title={`tool: ${sa.tool_name}`}>tool: {sa.tool_name}</Tag>}
            </Space>
          </div>
          {sa.summary && (
            <Text type="secondary" className="cc-text-small inv-sa-summary">{sa.summary}</Text>
          )}
          {Object.keys(sa.budget).length > 0 && (
            <div className="inv-sa-budget">
              {sa.budget.max_model_turns != null && <Tag size="small" className="cc-tag-meta">turns: {sa.budget.max_model_turns}</Tag>}
              {sa.budget.max_tool_calls != null && <Tag size="small" className="cc-tag-meta">calls: {sa.budget.max_tool_calls}</Tag>}
              {sa.budget.timeout_seconds != null && <Tag size="small" className="cc-tag-meta">timeout: {sa.budget.timeout_seconds}s</Tag>}
            </div>
          )}
        </div>
      ))}
      {workflows.length > 0 && (
        <div className="inv-wf-row">
          <Text type="secondary" className="cc-text-small">Workflows：</Text>
          <Space size={4} wrap>
            {workflows.map((w) => <Tag key={w} size="small" color="purple" title={w}>{w}</Tag>)}
          </Space>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// D. Prompts & context
// ---------------------------------------------------------------------------

function fileStatus(entry: FileEntry | null | undefined): React.ReactNode {
  if (!entry) return <Text type="secondary" className="cc-text-small">未配置</Text>;
  return (
    <Space size={4} wrap>
      <Tag size="small" color={entry.exists ? "green" : "gray"}>{entry.exists ? "存在" : "缺失"}</Tag>
      <Text className="cc-text-small" code>{entry.path}</Text>
    </Space>
  );
}

export function PromptConfigOverview({ config }: { config: BotInventory["config"] }) {
  const items: { label: string; value: React.ReactNode }[] = [];

  items.push({ label: "Persona", value: fileStatus(config.persona) });
  items.push({ label: "Refusal", value: fileStatus(config.refusal) });
  items.push({ label: "Safety", value: fileStatus(config.safety) });

  if (config.roles) {
    for (const [role, entry] of Object.entries(config.roles)) {
      items.push({ label: `Role: ${role}`, value: fileStatus(entry) });
    }
  } else {
    items.push({ label: "Roles", value: fileStatus(null) });
  }

  if (config.access) {
    items.push({
      label: "Access",
      value: (
        <Space size={4} wrap>
          {config.access.private_require_whitelist && <Tag size="small" className="cc-tag-meta">私聊白名单</Tag>}
          {config.access.group_require_whitelist && <Tag size="small" className="cc-tag-meta">群聊白名单</Tag>}
          {config.access.group_require_mention && <Tag size="small" className="cc-tag-meta">群聊需 @</Tag>}
          {!config.access.private_require_whitelist
            && !config.access.group_require_whitelist
            && !config.access.group_require_mention
            && <Text type="secondary" className="cc-text-small">未限制</Text>}
        </Space>
      ),
    });
  }

  return <Descriptions size="small" column={1} data={items} />;
}

export function ContextConfigOverview({ config }: { config: BotInventory["config"] }) {
  const items: { label: string; value: React.ReactNode }[] = [];

  items.push({
    label: "Memory Store",
    value: config.memory ? (
      <Space size={4} wrap>
        <Tag size="small" color="green" title={config.memory.provider}>{config.memory.provider}</Tag>
        {config.memory.namespace && <Text className="cc-text-small" code>{config.memory.namespace}</Text>}
        {config.memory.schema && <Text className="cc-text-small" type="secondary">schema: {config.memory.schema}</Text>}
      </Space>
    ) : <Text type="secondary" className="cc-text-small">未配置</Text>,
  });
  items.push({
    label: "RAG",
    value: config.rag ? <Text className="cc-text-small" code>{config.rag.sources}</Text> : <Text type="secondary" className="cc-text-small">未配置</Text>,
  });
  items.push({
    label: "Codebases",
    value: config.codebases ? <Text className="cc-text-small" code>{config.codebases.registry}</Text> : <Text type="secondary" className="cc-text-small">未配置</Text>,
  });
  items.push({
    label: "Playbooks",
    value: config.skills ? <Text className="cc-text-small" code>{config.skills.manifest}</Text> : <Text type="secondary" className="cc-text-small">未配置</Text>,
  });

  return <Descriptions size="small" column={1} data={items} />;
}

export function InventoryConfigOverview({ config }: { config: BotInventory["config"] }) {
  const items: { label: string; value: React.ReactNode }[] = [];

  items.push({ label: "Persona", value: fileStatus(config.persona) });
  items.push({ label: "Refusal", value: fileStatus(config.refusal) });
  if (config.safety) items.push({ label: "Safety", value: fileStatus(config.safety) });

  if (config.roles) {
    for (const [role, entry] of Object.entries(config.roles)) {
      items.push({ label: `Role: ${role}`, value: fileStatus(entry) });
    }
  }

  if (config.memory) {
    items.push({
      label: "Memory",
      value: (
        <Space size={4} wrap>
          <Tag size="small" color="green" title={config.memory.provider}>{config.memory.provider}</Tag>
          {config.memory.namespace && <Text className="cc-text-small" code>{config.memory.namespace}</Text>}
          {config.memory.schema && <Text className="cc-text-small" type="secondary">schema: {config.memory.schema}</Text>}
        </Space>
      ),
    });
  } else {
    items.push({ label: "Memory", value: <Text type="secondary" className="cc-text-small">—</Text> });
  }

  items.push({
    label: "RAG",
    value: config.rag
      ? <Text className="cc-text-small" code>{config.rag.sources}</Text>
      : <Text type="secondary" className="cc-text-small">—</Text>,
  });
  items.push({
    label: "Codebases",
    value: config.codebases
      ? <Text className="cc-text-small" code>{config.codebases.registry}</Text>
      : <Text type="secondary" className="cc-text-small">—</Text>,
  });
  items.push({
    label: "Skills",
    value: config.skills
      ? <Text className="cc-text-small" code>{config.skills.manifest}</Text>
      : <Text type="secondary" className="cc-text-small">—</Text>,
  });

  if (config.access) {
    items.push({
      label: "Access",
      value: (
        <Space size={4} wrap>
          {config.access.private_require_whitelist && <Tag size="small" className="cc-tag-meta">私聊白名单</Tag>}
          {config.access.group_require_whitelist && <Tag size="small" className="cc-tag-meta">群聊白名单</Tag>}
          {config.access.group_require_mention && <Tag size="small" className="cc-tag-meta">群聊需@</Tag>}
        </Space>
      ),
    });
  }

  return <Descriptions size="small" column={1} data={items} />;
}

// ---------------------------------------------------------------------------
// Main panel
// ---------------------------------------------------------------------------

export default function InventoryPanel({ data }: Props) {
  return (
    <Collapse defaultActiveKey={["tools", "prompts", "agents", "context"]}>
      <Collapse.Item
        header={
          <Space wrap>
            <Text bold>工具</Text>
            <Tag size="small">
              {data.tool_packs.length} 工具包 / {data.tool_features.length} 运行特性 / {data.mcp_services.length} MCP
            </Tag>
          </Space>
        }
        name="tools"
      >
        <Space direction="vertical" style={{ width: "100%" }} size={12}>
          <ToolPackSection items={data.tool_packs} hidden={data.hidden_tools} />
          <ToolFeatureSection items={data.tool_features} />
          <McpSection items={data.mcp_services} />
        </Space>
      </Collapse.Item>

      <Collapse.Item
        header={<Text bold>行为与提示词</Text>}
        name="prompts"
      >
        <PromptConfigOverview config={data.config} />
      </Collapse.Item>

      <Collapse.Item
        header={
          <Space wrap>
            <Text bold>Agent</Text>
            <Tag size="small">{data.agent_presets.length} agent / {data.workflows.length} workflow</Tag>
          </Space>
        }
        name="agents"
      >
        <SubagentSection subagents={data.agent_presets} workflows={data.workflows} />
      </Collapse.Item>

      <Collapse.Item header={<Text bold>上下文</Text>} name="context">
        <ContextConfigOverview config={data.config} />
      </Collapse.Item>
    </Collapse>
  );
}
