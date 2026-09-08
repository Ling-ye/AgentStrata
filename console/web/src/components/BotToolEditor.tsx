import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Alert, Button, Space, Switch, Tag } from "@arco-design/web-react";
import { api } from "../api";
import type { BotToolEditorProps } from "../features/bots/tool-editor/model";
import ToolPickerModal from "./ToolPickerModal";
import { useBotToolEditor } from "../features/bots/tool-editor/useBotToolEditor";
import ConfigurationPane from "../features/architecture/ConfigurationPane";
import { latestConfiguration } from "../features/architecture/configurationModel";
import { configurationTabForEntity, configurationViews, type ConfigurationTab, type DisplayEntity } from "../features/architecture/configurationPresentation";

export default function BotToolEditor(props: BotToolEditorProps & { view: ConfigurationTab; visible: boolean; running?: boolean }) {
  const { instanceId, view, visible } = props;
  const editor = useBotToolEditor(props);
  const inspection = useQuery({ queryKey: ["inspection", instanceId], queryFn: ({ signal }) => api.inspection(instanceId, undefined, undefined, signal),
    enabled: visible, refetchInterval: visible ? 5000 : false });
  const [selectedEntity, setSelectedEntity] = useState(() => new URLSearchParams(window.location.hash.split("?")[1]).get("entity") || "");
  const [revealVersion, setRevealVersion] = useState(0);
  const container = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const update = () => { setSelectedEntity(new URLSearchParams(window.location.hash.split("?")[1]).get("entity") || ""); setRevealVersion((value) => value + 1); };
    if (visible) update();
    window.addEventListener("hashchange", update);
    return () => window.removeEventListener("hashchange", update);
  }, [visible, view]);
  const expired = !!inspection.data?.loaded_meta && Date.now() / 1000 - inspection.data.loaded_meta.observed_at > 15;
  const data = inspection.data && (props.running === false || inspection.isError || expired) ? { ...inspection.data, loaded_stale: true, configuration_status: "unknown" as const,
    configuration_status_reason: props.running === false ? "服务已停止，运行状态暂无法确认" : "运行快照过期或读取失败，暂无法确认应用状态" } : inspection.data;
  const config = latestConfiguration(data);
  const entities = config ? configurationViews(config, editor.draft) : [];
  const navigate = (id: string) => {
    const params = new URLSearchParams(window.location.hash.split("?")[1]);
    params.set("instance", instanceId); params.set("tab", configurationTabForEntity(id)); params.set("entity", id);
    window.location.hash = "bots?" + params;
  };
  const remove = (label: string, action: () => void) => <Button size="mini" status="danger" disabled={editor.saving} onClick={action} aria-label={`移除 ${label}`}>移除</Button>;
  const actions = (entity: DisplayEntity) => {
    const name = entity.id.slice(entity.id.indexOf(":") + 1);
    const refs = entity.refs ?? [];
    return <Space wrap>
      {entity.id.startsWith("mcp:") && (() => {
        const ref = String(entity.config?.catalog_ref ?? entity.config?.ref ?? name);
        const server = editor.draft?.tools.mcp.servers.find((item) => item.ref === ref);
        const tool = entities.find((item) => item.group === "tools" && item.refs?.includes(entity.id));
        return <>{tool && <Button size="mini" onClick={() => navigate(tool.id)}>查看工具</Button>}{server && <>
          <Switch size="small" aria-label={`启用 ${name}`} disabled={editor.saving} checked={server.enabled} onChange={(enabled) => editor.toggleMcp(ref, enabled)} />
          {remove(name, () => editor.removeMcp(ref))}</>}</>;
      })()}
      {entity.id.startsWith("pack:") && editor.draft?.tools.packs.includes(name) && remove(name, () => editor.removeToolPack(name))}
      {entity.id.startsWith("feature:") && editor.draft?.tools.features.includes(name) && remove(name, () => editor.removeFeature(name))}
      {entity.id.startsWith("subagent:") && editor.draft?.agents.presets.includes(name) && remove(name, () => editor.removeAgentPreset(name))}
      {entity.id.startsWith("workflow:") && editor.draft?.agents.workflows.includes(name) && remove(name, () => editor.removeWorkflow(name))}
      {entity.id.startsWith("tool:") && refs.map((id) => <Button key={id} size="mini" onClick={() => navigate(id)}>{id.startsWith("mcp:") ? "服务器配置" : id.startsWith("subagent:") ? "委托配置" : "工具包配置"}</Button>)}
    </Space>;
  };
  const groupActions = (group: string) => <Space wrap>
    {group === "mcp" && <Button size="small" disabled={!editor.draft || editor.saving} onClick={() => editor.setPickerTarget("mcp")}>添加 MCP</Button>}
    {group === "delegation" && <><Button size="small" disabled={!editor.draft || editor.saving} onClick={() => editor.setPickerTarget("subagent")}>添加子 Agent</Button>
      <Button size="small" disabled={!editor.draft || editor.saving} onClick={() => editor.setPickerTarget("workflow")}>添加 Workflow</Button></>}
    {["packs", "features"].includes(group) && <Button size="small" disabled={!editor.draft || editor.saving} onClick={() => editor.setPickerTarget("capability")}>添加能力</Button>}
  </Space>;
  return <div ref={container} hidden={!visible} className="bot-tool-editor">
    {visible && <>
      {editor.error && <Alert type="error" content={"编辑配置读取失败：" + editor.error.message} />}
      {editor.dirty && <Alert type="warning" content="有未保存的配置修改，切换页签后仍保留。" />}
      <ConfigurationPane inspection={data} entities={entities} loading={inspection.isLoading} error={inspection.error}
        selectedEntity={selectedEntity} revealVersion={revealVersion} view={view} onRefresh={() => void inspection.refetch()}
        groupActions={groupActions} entityActions={actions} groupFooter={(group) => group === "tools" && !!editor.draft?.tools.hide.length && <div className="obs-hidden-tools"><h4>隐藏工具</h4>
          {editor.draft.tools.hide.map((name) => <div key={name}><Tag>{name}</Tag><Button size="mini" disabled={editor.saving} onClick={() => editor.removeHiddenTool(name)}>恢复 {name}</Button></div>)}</div>} />
      <div className="obs-config-save"><span>{editor.dirty ? "有未保存修改" : "配置已保存"}</span><Space wrap>
        <Button type="primary" loading={editor.saving} disabled={!editor.dirty} onClick={() => void editor.handleSave(false)}>保存配置</Button>
        <Button loading={editor.saving} disabled={!editor.dirty && data?.configuration_status !== "pending"} onClick={() => void editor.handleSave(true)}>保存并重启</Button>
      </Space></div>
      <ToolPickerModal visible={editor.pickerTarget !== null} title={editor.pickerTarget === "mcp" ? "添加 MCP" : editor.pickerTarget === "subagent" ? "添加子 Agent" : editor.pickerTarget === "workflow" ? "添加 Workflow" : "添加能力"}
        items={editor.pickerItems} selected={editor.pickerSelected} onConfirm={editor.handlePickerConfirm} onCancel={() => editor.setPickerTarget(null)} />
    </>}
  </div>;
}
