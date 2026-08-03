import { Alert, Button, Space, Spin, Switch, Tabs, Tag, Typography } from "@arco-design/web-react";
import { IconDelete, IconPlus } from "@arco-design/web-react/icon";
import { healthTagColor, infraStateLabel } from "../shared/ui/status";
import {
  SURFACE_TEXT,
  type BotToolEditorProps,
  type SurfaceKey,
} from "../features/bots/tool-editor/model";
import { PromptConfigOverview, ContextConfigOverview, RiskBadge } from "./InventoryPanel";
import ToolPickerModal from "./ToolPickerModal";
import { useBotToolEditor } from "../features/bots/tool-editor/useBotToolEditor";

const { Text, Title } = Typography;

export default function BotToolEditor({ instanceId, isDeployed = false, inventory, onApplyTask }: BotToolEditorProps) {
  const {
    activeSurface,
    catalogByKind,
    dirty,
    draft,
    featureList,
    handlePickerConfirm,
    handleSave,
    isLoading,
    mcpByRef,
    mcpCatalogByRef,
    pickerItems,
    pickerSelected,
    pickerTarget,
    removeAgentPreset,
    removeFeature,
    removeHiddenTool,
    removeMcp,
    removeToolPack,
    removeWorkflow,
    saving,
    setActiveSurface,
    setPickerTarget,
    subagentByName,
    toggleMcp,
    toolPackById,
  } = useBotToolEditor({ instanceId, isDeployed, inventory, onApplyTask });

  if (isLoading || !draft) {
    return <div className="panel-spinner"><Spin /></div>;
  }

  return (
    <div className="bot-tool-editor">
      <Tabs
        type="capsule"
        size="small"
        activeTab={activeSurface}
        onChange={(key) => setActiveSurface(key as SurfaceKey)}
      >
        <Tabs.TabPane
          key="tools"
          title={`${SURFACE_TEXT.tools} ${draft.tools.packs.length + featureList.length + draft.tools.mcp.servers.length}`}
        >
          <div className="tool-surface-heading">
            <Title heading={6} style={{ margin: 0 }}>{SURFACE_TEXT.tools}</Title>
            <Text type="secondary" className="cc-text-small">{SURFACE_TEXT.toolsHelp}</Text>
          </div>

          <div className="tool-section">
            <Space className="tool-section-header">
              <Title heading={6} style={{ margin: 0 }}>{SURFACE_TEXT.localCapabilities}</Title>
              <Button size="small" icon={<IconPlus />} onClick={() => setPickerTarget("capability")}>
                {SURFACE_TEXT.addCapability}
              </Button>
            </Space>
            <div className="tool-item-list">
              {draft.tools.packs.map((cap) => {
                const info = catalogByKind.tool_pack?.find((i) => i.name === cap);
                const inv = toolPackById.get(cap);
                const description = inv?.description || info?.description || "";
                const hasTools = inv?.has_tools ?? info?.has_tools;
                const hasPrompts = inv?.has_prompts ?? info?.has_prompts;
                return (
                  <div key={cap} className="tool-item-row tool-item-row-rich">
                    <div className="tool-item-content">
                      <div className="tool-item-title-row">
                        <Text code className="cc-text-small tool-item-code">{cap}</Text>
                        <Space size={4} wrap>
                          <Tag size="small" color="arcoblue">Pack</Tag>
                          {inv?.label && <Tag size="small" color="light-blue" title={inv.label}>{inv.label}</Tag>}
                          {hasTools && <Tag size="small" className="cc-tag-meta">{SURFACE_TEXT.toolTag}</Tag>}
                          {hasPrompts && <Tag size="small" className="cc-tag-meta">Prompt</Tag>}
                        </Space>
                      </div>
                      {description && (
                        <Text type="secondary" className="cc-text-small tool-item-description">
                          {description}
                        </Text>
                      )}
                    </div>
                    <Button
                      size="mini"
                      type="text"
                      status="danger"
                      icon={<IconDelete />}
                      className="tool-item-action"
                      onClick={() => removeToolPack(cap)}
                    />
                  </div>
                );
              })}
              {featureList.map((feature) => {
                const info = catalogByKind.tool_feature?.find((i) => i.name === feature);
                return (
                  <div key={feature} className="tool-item-row tool-item-row-rich">
                    <div className="tool-item-content">
                      <div className="tool-item-title-row">
                        <Text code className="cc-text-small tool-item-code">{feature}</Text>
                        <Tag size="small" color="green">{SURFACE_TEXT.runtimeFeatures}</Tag>
                        {info?.category && <Tag size="small" color="light-blue" title={info.category}>{info.category}</Tag>}
                      </div>
                      {info?.description && (
                        <Text type="secondary" className="cc-text-small tool-item-description">
                          {info.description}
                        </Text>
                      )}
                    </div>
                    <Button
                      size="mini"
                      type="text"
                      status="danger"
                      icon={<IconDelete />}
                      className="tool-item-action"
                      onClick={() => removeFeature(feature)}
                    />
                  </div>
                );
              })}
              {draft.tools.packs.length === 0 && featureList.length === 0 && (
                <Text type="secondary">{SURFACE_TEXT.empty}</Text>
              )}
            </div>
          </div>

          <div className="tool-section">
            <Space className="tool-section-header">
              <Title heading={6} style={{ margin: 0 }}>{SURFACE_TEXT.mcpServices}</Title>
              <Button size="small" icon={<IconPlus />} onClick={() => setPickerTarget("mcp")}>
                {SURFACE_TEXT.addMcp}
              </Button>
            </Space>
            <div className="tool-item-list">
              {draft.tools.mcp.servers.map((srv) => {
                const info = mcpCatalogByRef.get(srv.ref);
                const inv = mcpByRef.get(srv.ref);
                const title = inv?.title || info?.name || srv.ref;
                const risk = inv?.risk || info?.risk || "";
                return (
                  <div key={srv.ref} className="tool-item-row tool-item-row-rich">
                    <Switch
                      size="small"
                      checked={srv.enabled}
                      onChange={(on) => toggleMcp(srv.ref, on)}
                    />
                    <div className="tool-item-content">
                      <div className="tool-item-title-row">
                        <Text code className="cc-text-small tool-item-code">{srv.ref}</Text>
                        <Text bold className="cc-text-small">{title}</Text>
                        <Space size={4} wrap>
                          {!srv.enabled && <Tag size="small" color="gray">{SURFACE_TEXT.disabled}</Tag>}
                          <RiskBadge risk={risk} />
                          {inv?.infra_state && (
                            <Tag size="small" className="cc-status-tag" color={healthTagColor(inv.infra_color)}>
                              {infraStateLabel(inv.infra_state)}
                            </Tag>
                          )}
                        </Space>
                      </div>
                      <div className="tool-item-meta">
                        {inv?.transport && <Tag size="small" className="cc-tag-meta" title={inv.transport}>{inv.transport}</Tag>}
                        {inv?.exposure && <Tag size="small" color="blue" title={inv.exposure}>{inv.exposure}</Tag>}
                        {inv?.infra_service_id && <Tag size="small" className="cc-tag-meta" title={`infra: ${inv.infra_service_id}`}>infra: {inv.infra_service_id}</Tag>}
                        {inv?.allowed_subagents.map((name) => (
                          <Tag key={name} size="small" color="cyan" title={name}>{name}</Tag>
                        ))}
                      </div>
                    </div>
                    <Button
                      size="mini"
                      type="text"
                      status="danger"
                      icon={<IconDelete />}
                      className="tool-item-action"
                      onClick={() => removeMcp(srv.ref)}
                    />
                  </div>
                );
              })}
              {draft.tools.mcp.servers.length === 0 && <Text type="secondary">{SURFACE_TEXT.empty}</Text>}
            </div>
          </div>

          {draft.tools.hide.length > 0 && (
            <div className="tool-section">
              <Title heading={6} style={{ margin: 0 }}>{SURFACE_TEXT.hiddenTools}</Title>
              <div className="tool-item-list">
                {draft.tools.hide.map((toolName) => (
                  <div key={toolName} className="tool-item-row tool-item-row-rich">
                    <div className="tool-item-content">
                      <Text code className="cc-text-small tool-item-code">{toolName}</Text>
                    </div>
                    <Button
                      size="mini"
                      type="text"
                      status="danger"
                      icon={<IconDelete />}
                      className="tool-item-action"
                      onClick={() => removeHiddenTool(toolName)}
                    />
                  </div>
                ))}
              </div>
            </div>
          )}
        </Tabs.TabPane>

        <Tabs.TabPane key="prompts" title={SURFACE_TEXT.prompts}>
          <div className="tool-surface-heading">
            <Title heading={6} style={{ margin: 0 }}>{SURFACE_TEXT.prompts}</Title>
            <Text type="secondary" className="cc-text-small">{SURFACE_TEXT.promptsHelp}</Text>
          </div>
          <div className="tool-section">
            {inventory ? (
              <PromptConfigOverview config={inventory.config} />
            ) : (
              <Alert type="info" content={SURFACE_TEXT.promptsLoading} />
            )}
          </div>
        </Tabs.TabPane>

        <Tabs.TabPane
          key="agents"
          title={`Agent ${draft.agents.presets.length + draft.agents.workflows.length}`}
        >
          <div className="tool-surface-heading">
            <Title heading={6} style={{ margin: 0 }}>Agent</Title>
            <Text type="secondary" className="cc-text-small">{SURFACE_TEXT.agentsHelp}</Text>
          </div>

          <div className="tool-section">
            <Space className="tool-section-header">
              <Title heading={6} style={{ margin: 0 }}>{SURFACE_TEXT.subagents}</Title>
              <Button size="small" icon={<IconPlus />} onClick={() => setPickerTarget("subagent")}>
                {SURFACE_TEXT.addSubagent}
              </Button>
            </Space>
            <div className="tool-item-list">
              {draft.agents.presets.map((name) => {
                const info = catalogByKind.subagent?.find((i) => i.name === name);
                const inv = subagentByName.get(name);
                const summary = inv?.summary || info?.description || "";
                const budget = inv?.budget ?? {};
                return (
                  <div key={name} className="tool-item-row tool-item-row-rich">
                    <div className="tool-item-content">
                      <div className="tool-item-title-row">
                        <Text code className="cc-text-small tool-item-code">{name}</Text>
                        <Space size={4} wrap>
                          {inv?.kind && <Tag size="small" color="blue" title={inv.kind}>{inv.kind}</Tag>}
                          {inv?.tool_name && <Tag size="small" className="cc-tag-meta" title={`tool: ${inv.tool_name}`}>tool: {inv.tool_name}</Tag>}
                          {(inv?.workflow_tags ?? []).map((tag) => (
                            <Tag key={tag} size="small" color="purple" title={tag}>{tag}</Tag>
                          ))}
                        </Space>
                      </div>
                      {summary && (
                        <Text type="secondary" className="cc-text-small tool-item-description">
                          {summary}
                        </Text>
                      )}
                      <div className="tool-item-meta">
                        {budget.max_model_turns != null && <Tag size="small" className="cc-tag-meta">turns: {budget.max_model_turns}</Tag>}
                        {budget.max_tool_calls != null && <Tag size="small" className="cc-tag-meta">calls: {budget.max_tool_calls}</Tag>}
                        {budget.timeout_seconds != null && <Tag size="small" className="cc-tag-meta">timeout: {budget.timeout_seconds}s</Tag>}
                        {budget.max_output_chars != null && <Tag size="small" className="cc-tag-meta">output: {budget.max_output_chars}</Tag>}
                      </div>
                    </div>
                    <Button
                      size="mini"
                      type="text"
                      status="danger"
                      icon={<IconDelete />}
                      className="tool-item-action"
                      onClick={() => removeAgentPreset(name)}
                    />
                  </div>
                );
              })}
              {draft.agents.presets.length === 0 && <Text type="secondary">{SURFACE_TEXT.empty}</Text>}
            </div>
          </div>

          <div className="tool-section">
            <Space className="tool-section-header">
              <Title heading={6} style={{ margin: 0 }}>Workflow</Title>
              <Button size="small" icon={<IconPlus />} onClick={() => setPickerTarget("workflow")}>
                {SURFACE_TEXT.addWorkflow}
              </Button>
            </Space>
            <div className="tool-item-list">
              {draft.agents.workflows.map((name) => {
                const info = catalogByKind.workflow?.find((i) => i.name === name);
                return (
                  <div key={name} className="tool-item-row tool-item-row-rich">
                    <div className="tool-item-content">
                      <div className="tool-item-title-row">
                        <Text code className="cc-text-small tool-item-code">{name}</Text>
                        {info?.category && <Tag size="small" color="blue" title={info.category}>{info.category}</Tag>}
                      </div>
                      {info?.description && (
                        <Text type="secondary" className="cc-text-small tool-item-description">
                          {info.description}
                        </Text>
                      )}
                    </div>
                    <Button
                      size="mini"
                      type="text"
                      status="danger"
                      icon={<IconDelete />}
                      className="tool-item-action"
                      onClick={() => removeWorkflow(name)}
                    />
                  </div>
                );
              })}
              {draft.agents.workflows.length === 0 && <Text type="secondary">{SURFACE_TEXT.empty}</Text>}
            </div>
          </div>
        </Tabs.TabPane>

        <Tabs.TabPane key="context" title={SURFACE_TEXT.context}>
          <div className="tool-surface-heading">
            <Title heading={6} style={{ margin: 0 }}>{SURFACE_TEXT.context}</Title>
            <Text type="secondary" className="cc-text-small">{SURFACE_TEXT.contextHelp}</Text>
          </div>
          <div className="tool-section">
            {inventory ? (
              <ContextConfigOverview config={inventory.config} />
            ) : (
              <Alert type="info" content={SURFACE_TEXT.contextLoading} />
            )}
          </div>
        </Tabs.TabPane>
      </Tabs>

      {dirty && (
        <Alert
          type="warning"
          content="配置已修改。「保存并重启」将把配置同步到运行实例目录并重启服务。"
          style={{ marginTop: 12 }}
        />
      )}

      {/* Action bar */}
      <div className="card-action-row" style={{ marginTop: 12 }}>
        <Space>
          <Button type="primary" loading={saving} disabled={!dirty} onClick={() => handleSave(false)}>
            保存配置
          </Button>
          <Button type="primary" status="success" loading={saving} disabled={!dirty} onClick={() => handleSave(true)}>
            保存并重启
          </Button>
        </Space>
      </div>

      <ToolPickerModal
        visible={pickerTarget !== null}
        title={
          pickerTarget === "capability" ? "添加能力"
            : pickerTarget === "mcp" ? "添加 MCP 服务"
              : pickerTarget === "subagent" ? "添加子代理"
                : pickerTarget === "workflow" ? "添加 Workflow"
                : ""
        }
        items={pickerItems}
        selected={pickerSelected}
        onConfirm={handlePickerConfirm}
        onCancel={() => setPickerTarget(null)}
      />
    </div>
  );
}
