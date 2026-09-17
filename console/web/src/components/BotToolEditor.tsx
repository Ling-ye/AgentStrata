import { useContext, useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import { Alert, Button, Modal, Space, Switch, Tag } from "@arco-design/web-react";
import { api } from "../api";
import type { BotToolEditorProps } from "../features/bots/tool-editor/model";
import ToolPickerModal from "./ToolPickerModal";
import { useBotToolEditor } from "../features/bots/tool-editor/useBotToolEditor";
import ConfigurationPane from "../features/architecture/ConfigurationPane";
import { currentInspection, latestConfiguration } from "../features/architecture/configurationModel";
import { configurationRelated, configurationViews, type ConfigurationGroup, type DisplayEntity } from "../features/architecture/configurationPresentation";
import { leavesBotInstance, NavigationGuardContext } from "../shared/navigationGuard";

export default function BotToolEditor(props: BotToolEditorProps & { visible: boolean; running?: boolean; infoVisible: boolean; onInfoClose: () => void }) {
  const { instanceId, visible, infoVisible } = props;
  const editor = useBotToolEditor(props);
  const registerGuard = useContext(NavigationGuardContext);
  const inspection = useQuery({ queryKey: ["inspection", instanceId], queryFn: ({ signal }) => api.inspection(instanceId, undefined, undefined, signal),
    enabled: visible || infoVisible, refetchInterval: visible || infoVisible ? 5000 : false });
  useEffect(() => registerGuard({
    shouldBlock: (hash) => editor.dirty && leavesBotInstance(hash, instanceId),
    confirm: () => new Promise<boolean>((resolve) => {
      Modal.confirm({ title: "有未保存的配置修改", content: "离开将丢弃当前实例的草稿。已启动的应用任务会继续执行。",
        okText: "放弃并离开", cancelText: "继续编辑", onOk: () => { editor.discard(); resolve(true); }, onCancel: () => resolve(false) });
    }),
  }), [registerGuard, editor.dirty, editor.discard, instanceId]);
  useEffect(() => {
    if (!editor.dirty) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ""; };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [editor.dirty]);

  const data = currentInspection(inspection.data, { running: props.running, failed: inspection.isError, applying: editor.applying });
  useEffect(() => {
    if (data?.configuration_status === "applied") editor.acknowledgeApplied();
  }, [inspection.dataUpdatedAt, data?.configuration_status, editor.acknowledgeApplied]);
  const config = latestConfiguration(data);
  const entities = config ? configurationViews(config, editor.draft) : [];
  const addingDisabled = !editor.draft || editor.saving || editor.catalogLoading || !!editor.catalogError;
  const remove = (label: string, action: () => void) => <Button size="small" status="danger" disabled={editor.saving} onClick={action} aria-label={`移除 ${label}`}>移除</Button>;
  const actions = (entity: DisplayEntity, navigate: (id: string) => void) => {
    const name = entity.id.slice(entity.id.indexOf(":") + 1);
    const related = configurationRelated(entities, entity);
    return <Space wrap>
      {entity.id.startsWith("mcp:") && (() => {
        const ref = String(entity.config?.catalog_ref ?? entity.config?.ref ?? name);
        const server = editor.draft?.tools.mcp.servers.find((item) => item.ref === ref);
        return server && <><Switch size="small" aria-label={`启用 ${name}`} checkedText="启用" uncheckedText="停用" disabled={editor.saving}
          checked={server.enabled} onChange={(enabled) => editor.toggleMcp(ref, enabled)} />{remove(name, () => editor.removeMcp(ref))}</>;
      })()}
      {entity.id.startsWith("pack:") && editor.draft?.tools.packs.includes(name) && remove(name, () => editor.removeToolPack(name))}
      {entity.id.startsWith("feature:") && editor.draft?.tools.features.includes(name) && remove(name, () => editor.removeFeature(name))}
      {entity.id.startsWith("subagent:") && editor.draft?.agents.presets.includes(name) && remove(name, () => editor.removeAgentPreset(name))}
      {entity.id.startsWith("workflow:") && editor.draft?.agents.workflows.includes(name) && remove(name, () => editor.removeWorkflow(name))}
      {related.map((item) => <Button key={item.viewKey} size="small" onClick={() => navigate(item.id)}>查看 {item.name}</Button>)}
    </Space>;
  };
  const groupActions = (group: ConfigurationGroup) => <Space wrap>
    {group === "mcp" && <Button size="small" disabled={addingDisabled} onClick={() => editor.setPickerTarget("mcp")}>添加 MCP</Button>}
    {group === "delegation" && <><Button size="small" disabled={addingDisabled} onClick={() => editor.setPickerTarget("subagent")}>添加子 Agent</Button>
      {editor.catalogByKind.workflow.some((item) => !editor.draft?.agents.workflows.includes(item.name)) &&
        <Button size="small" disabled={addingDisabled} onClick={() => editor.setPickerTarget("workflow")}>添加 Workflow</Button>}</>}
    {group === "packs" && <Button size="small" disabled={addingDisabled} onClick={() => editor.setPickerTarget("tool_pack")}>添加工具包</Button>}
    {group === "features" && <Button size="small" disabled={addingDisabled} onClick={() => editor.setPickerTarget("tool_feature")}>添加运行特性</Button>}
  </Space>;
  const pending = data?.configuration_status === "pending" || editor.unappliedSave;
  return <div className="bot-tool-editor" hidden={!visible && !infoVisible}>
    {visible && <>
      {editor.error && <Alert type="error" content={"编辑配置读取失败：" + editor.error.message} />}
      {editor.catalogError && <Alert type="warning" content="组件目录读取失败，暂时无法添加组件。" />}
      {editor.saveError && <Alert type="error" content={editor.saveError} />}
      {editor.dirty && <Alert type="warning" content="有未保存的配置修改，切换层和页签后仍保留。" />}
    </>}
    <ConfigurationPane instanceId={instanceId} visible={visible} infoVisible={infoVisible} onInfoClose={props.onInfoClose}
      inspection={data} entities={entities} loading={inspection.isLoading} error={inspection.error}
      onRefresh={() => { void inspection.refetch(); }} groupActions={groupActions} entityActions={actions}
      groupFooter={(group) => group === "tools" && !!editor.draft?.tools.hide.length && <div className="obs-hidden-tools"><h4>隐藏工具</h4>
        {editor.draft.tools.hide.map((name) => <div key={name}><Tag>{name}</Tag><Button size="small" disabled={editor.saving} onClick={() => editor.removeHiddenTool(name)}>恢复 {name}</Button></div>)}</div>} />
    {visible && (editor.dirty || pending || editor.applying) && <div className="obs-config-save">
      <span>{editor.applying ? "应用任务执行中" : editor.dirty ? "有未保存修改" : "已保存，待应用"}
        {!props.isDeployed && <small>实例尚未部署，部署后生效。</small>}</span>
      <Space wrap>
        {editor.dirty && <><Button disabled={editor.saving} onClick={editor.discard}>放弃修改</Button>
          <Button type="primary" loading={editor.saving} onClick={() => void editor.handleSave(false)}>保存配置</Button></>}
        {props.isDeployed && <Button loading={editor.saving} disabled={!editor.draft} onClick={() => void editor.handleSave(true)}>
          {editor.dirty ? "保存并重启" : "应用并重启"}</Button>}
      </Space>
    </div>}
    <ToolPickerModal visible={visible && editor.pickerTarget !== null}
      title={editor.pickerTarget === "mcp" ? "添加 MCP" : editor.pickerTarget === "subagent" ? "添加子 Agent" : editor.pickerTarget === "workflow" ? "添加 Workflow" : editor.pickerTarget === "tool_feature" ? "添加运行特性" : "添加工具包"}
      items={editor.pickerItems} selected={editor.pickerSelected} onConfirm={editor.handlePickerConfirm} onCancel={() => editor.setPickerTarget(null)} />
  </div>;
}
