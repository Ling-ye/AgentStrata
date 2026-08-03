import { useCallback, useEffect, useMemo, useState } from "react";
import { Message } from "@arco-design/web-react";

import { api } from "../../../api";
import { useBotToolConfig, useCatalog } from "../../catalog/useCatalog";
import type { BotToolConfig, McpServerRef } from "../../../types";
import {
  groupCatalog,
  indexBy,
  type BotToolEditorProps,
  type PickerTarget,
  type SurfaceKey,
} from "./model";

export function useBotToolEditor({
  instanceId,
  isDeployed = false,
  inventory,
  onApplyTask,
}: BotToolEditorProps) {
  const { data: catalogData } = useCatalog();
  const { data: toolConfig, isLoading, refetch } = useBotToolConfig(instanceId);
  const [draft, setDraft] = useState<BotToolConfig | null>(null);
  const [saving, setSaving] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [pickerTarget, setPickerTarget] = useState<PickerTarget>(null);
  const [activeSurface, setActiveSurface] = useState<SurfaceKey>("tools");

  useEffect(() => {
    if (toolConfig && !dirty) setDraft({ ...toolConfig });
  }, [toolConfig, dirty]);

  const catalogByKind = useMemo(() => groupCatalog(catalogData), [catalogData]);
  const toolPackById = useMemo(
    () => indexBy(inventory?.tool_packs, (item) => item.id),
    [inventory],
  );
  const mcpByRef = useMemo(
    () => indexBy(inventory?.mcp_services, (item) => item.ref),
    [inventory],
  );
  const subagentByName = useMemo(
    () => indexBy(inventory?.agent_presets, (item) => item.name),
    [inventory],
  );
  const mcpCatalogByRef = useMemo(
    () => indexBy(catalogByKind.mcp, (item) => item.id.replace(/^mcp:/, "")),
    [catalogByKind.mcp],
  );

  const removeToolPack = useCallback((pack: string) => {
    setDraft((current) => current ? {
      ...current,
      tools: { ...current.tools, packs: current.tools.packs.filter((item) => item !== pack) },
    } : current);
    setDirty(true);
  }, []);

  const removeFeature = useCallback((feature: string) => {
    setDraft((current) => current ? {
      ...current,
      tools: { ...current.tools, features: current.tools.features.filter((item) => item !== feature) },
    } : current);
    setDirty(true);
  }, []);

  const removeHiddenTool = useCallback((toolName: string) => {
    setDraft((current) => current ? {
      ...current,
      tools: { ...current.tools, hide: current.tools.hide.filter((item) => item !== toolName) },
    } : current);
    setDirty(true);
  }, []);

  const removeMcp = useCallback((ref: string) => {
    setDraft((current) => current ? {
      ...current,
      tools: {
        ...current.tools,
        mcp: { servers: current.tools.mcp.servers.filter((item) => item.ref !== ref) },
      },
    } : current);
    setDirty(true);
  }, []);

  const toggleMcp = useCallback((ref: string, enabled: boolean) => {
    setDraft((current) => current ? {
      ...current,
      tools: {
        ...current.tools,
        mcp: {
          servers: current.tools.mcp.servers.map(
            (item) => item.ref === ref ? { ...item, enabled } : item,
          ),
        },
      },
    } : current);
    setDirty(true);
  }, []);

  const removeAgentPreset = useCallback((name: string) => {
    setDraft((current) => current ? {
      ...current,
      agents: { ...current.agents, presets: current.agents.presets.filter((item) => item !== name) },
    } : current);
    setDirty(true);
  }, []);

  const removeWorkflow = useCallback((name: string) => {
    setDraft((current) => current ? {
      ...current,
      agents: { ...current.agents, workflows: current.agents.workflows.filter((item) => item !== name) },
    } : current);
    setDirty(true);
  }, []);

  const handlePickerConfirm = useCallback((added: string[]) => {
    if (!draft || !pickerTarget) return;
    if (pickerTarget === "capability") {
      const packs = added.filter((name) => catalogByKind.tool_pack.some((item) => item.name === name));
      const features = added.filter((name) => catalogByKind.tool_feature.some((item) => item.name === name));
      setDraft({
        ...draft,
        tools: {
          ...draft.tools,
          packs: [...draft.tools.packs, ...packs],
          features: [...draft.tools.features, ...features],
        },
      });
    } else if (pickerTarget === "mcp") {
      const servers: McpServerRef[] = added.map((name) => {
        const item = catalogByKind.mcp.find((candidate) => candidate.name === name);
        return { ref: item?.id.replace("mcp:", "") ?? name, enabled: true };
      });
      setDraft({
        ...draft,
        tools: { ...draft.tools, mcp: { servers: [...draft.tools.mcp.servers, ...servers] } },
      });
    } else if (pickerTarget === "subagent") {
      setDraft({ ...draft, agents: { ...draft.agents, presets: [...draft.agents.presets, ...added] } });
    } else {
      setDraft({ ...draft, agents: { ...draft.agents, workflows: [...draft.agents.workflows, ...added] } });
    }
    setDirty(true);
    setPickerTarget(null);
  }, [draft, pickerTarget, catalogByKind]);

  const handleSave = useCallback(async (apply: boolean) => {
    if (!draft) return;
    setSaving(true);
    try {
      const result = await api.updateBotTools(instanceId, draft, { apply: apply && isDeployed });
      if ("warnings" in result && result.warnings?.length) Message.warning(result.warnings.join("; "));
      if ("id" in result) {
        Message.info("保存并更新任务已启动…");
        onApplyTask?.(result, () => {
          setDirty(false);
          void refetch();
        });
        return;
      }
      Message.success("配置已保存");
      setDirty(false);
      void refetch();
      if (apply && !isDeployed) Message.warning("实例尚未部署，配置仅写入源仓。");
    } catch (error) {
      Message.error(`保存失败: ${error instanceof Error ? error.message : String(error)}`);
    } finally {
      setSaving(false);
    }
  }, [draft, instanceId, isDeployed, onApplyTask, refetch]);

  const toolPackSet = new Set(draft?.tools.packs ?? []);
  const featureList = draft?.tools.features ?? [];
  const featureSet = new Set(featureList);
  const mcpRefSet = new Set(draft?.tools.mcp.servers.map((item) => item.ref) ?? []);
  const agentPresetSet = new Set(draft?.agents.presets ?? []);
  const workflowSet = new Set(draft?.agents.workflows ?? []);
  const pickerItems = pickerTarget === "capability"
    ? [...catalogByKind.tool_pack, ...catalogByKind.tool_feature]
    : pickerTarget === "mcp" ? catalogByKind.mcp
      : pickerTarget === "subagent" ? catalogByKind.subagent
        : pickerTarget === "workflow" ? catalogByKind.workflow : [];
  const pickerSelected = pickerTarget === "capability"
    ? new Set([...toolPackSet, ...featureSet])
    : pickerTarget === "mcp"
      ? new Set(Array.from(mcpRefSet).map((ref) => mcpCatalogByRef.get(ref)?.name ?? ref))
      : pickerTarget === "subagent" ? agentPresetSet
        : pickerTarget === "workflow" ? workflowSet : new Set<string>();

  return {
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
  };
}
