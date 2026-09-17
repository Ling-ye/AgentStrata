import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Message } from "@arco-design/web-react";
import { api } from "../../../api";
import { useBotToolConfig, useCatalog } from "../../catalog/useCatalog";
import type { BotToolConfig } from "../../../types";
import { groupCatalog, indexBy, type BotToolEditorProps, type PickerTarget } from "./model";
import { draftIsDirty, draftReducer } from "./draftModel";

export function useBotToolEditor({ instanceId, isDeployed = false, onApplyTask }: BotToolEditorProps) {
  const catalog = useCatalog();
  const { data: toolConfig, isLoading, refetch, error } = useBotToolConfig(instanceId);
  const [state, dispatch] = useReducer(draftReducer, { saved: null, draft: null });
  const { draft } = state;
  const dirty = draftIsDirty(state);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [unappliedSave, setUnappliedSave] = useState(false);
  const acknowledgeApplied = useCallback(() => setUnappliedSave(false), []);
  const [apply, setApply] = useState<{ id: string; submitted: BotToolConfig } | null>(null);
  const [pickerTarget, setPickerTarget] = useState<PickerTarget>(null);
  const queryClient = useQueryClient();
  const scope = useRef<object>({});
  useEffect(() => { scope.current = {}; return () => { scope.current = {}; }; }, [instanceId]);
  const busy = saving || !!apply;
  useEffect(() => { if (toolConfig && !busy) dispatch({ type: "receive", config: toolConfig }); }, [toolConfig, busy]);
  const catalogByKind = useMemo(() => groupCatalog(catalog.data), [catalog.data]);
  const mcpCatalogByRef = useMemo(() => indexBy(catalogByKind.mcp, (item) => item.id.replace(/^mcp:/, "")), [catalogByKind.mcp]);
  const applyQuery = useQuery({ queryKey: ["configuration-apply", instanceId, apply?.id],
    queryFn: () => api.task(apply!.id), enabled: !!apply, refetchInterval: apply ? 1000 : false });

  const invalidate = useCallback(async () => {
    await Promise.all(["inspection", "bot-inventory", "bot-status"].map((key) => queryClient.invalidateQueries({ queryKey: [key, instanceId] })));
  }, [queryClient, instanceId]);
  const refreshSaved = useCallback(async (submitted: BotToolConfig, requestScope: object) => {
    const refreshed = await refetch();
    if (scope.current !== requestScope) return;
    const config = refreshed.isSuccess ? refreshed.data : submitted;
    if (!refreshed.isSuccess) queryClient.setQueryData(["bot-tools", instanceId], submitted);
    dispatch({ type: "saved", submitted, config });
    await invalidate();
  }, [refetch, invalidate, queryClient, instanceId]);

  useEffect(() => {
    const result = applyQuery.data;
    if (!apply || !result || !["done", "failed"].includes(result.status)) return;
    const requestScope = scope.current;
    const submitted = apply.submitted;
    setApply(null);
    if (result.status === "done") {
      setSaving(true);
      void refreshSaved(submitted, requestScope).finally(() => { if (scope.current === requestScope) setSaving(false); });
    } else {
      setSaveError("应用任务失败，修改已保留。请查看任务日志，并刷新确认源配置与运行状态。");
      // An apply task can fail after writing the files. Refresh the saved baseline
      // without treating the requested draft as an acknowledged successful write.
      void Promise.all([refetch(), invalidate()]);
    }
  }, [apply, applyQuery.data, refreshSaved, invalidate, refetch]);

  const change = useCallback((update: (current: BotToolConfig) => BotToolConfig) => { dispatch({ type: "change", update }); setSaveError(null); }, []);
  const removeToolPack = (pack: string) => change((current) => ({ ...current, tools: { ...current.tools, packs: current.tools.packs.filter((item) => item !== pack) } }));
  const removeFeature = (feature: string) => change((current) => ({ ...current, tools: { ...current.tools, features: current.tools.features.filter((item) => item !== feature) } }));
  const removeHiddenTool = (name: string) => change((current) => ({ ...current, tools: { ...current.tools, hide: current.tools.hide.filter((item) => item !== name) } }));
  const removeMcp = (ref: string) => change((current) => ({ ...current, tools: { ...current.tools, mcp: { servers: current.tools.mcp.servers.filter((item) => item.ref !== ref) } } }));
  const toggleMcp = (ref: string, enabled: boolean) => change((current) => ({ ...current, tools: { ...current.tools, mcp: { servers: current.tools.mcp.servers.map((item) => item.ref === ref ? { ...item, enabled } : item) } } }));
  const removeAgentPreset = (name: string) => change((current) => ({ ...current, agents: { ...current.agents, presets: current.agents.presets.filter((item) => item !== name) } }));
  const removeWorkflow = (name: string) => change((current) => ({ ...current, agents: { ...current.agents, workflows: current.agents.workflows.filter((item) => item !== name) } }));
  const discard = useCallback(() => { dispatch({ type: "discard" }); setSaveError(null); }, []);

  const handlePickerConfirm = (added: string[]) => {
    if (!draft || !pickerTarget || busy) return;
    change((current) => {
      if (pickerTarget === "tool_pack" || pickerTarget === "tool_feature") {
        const key = pickerTarget === "tool_pack" ? "packs" : "features";
        const allowed = added.filter((name) => catalogByKind[pickerTarget].some((item) => item.name === name));
        return { ...current, tools: { ...current.tools, [key]: [...new Set([...current.tools[key], ...allowed])] } };
      }
      if (pickerTarget === "mcp") {
        const servers = added.flatMap((name) => {
          const item = catalogByKind.mcp.find((candidate) => candidate.name === name);
          return item ? [{ ref: item.id.replace(/^mcp:/, ""), enabled: true }] : [];
        });
        return { ...current, tools: { ...current.tools, mcp: { servers: [...current.tools.mcp.servers, ...servers] } } };
      }
      const key = pickerTarget === "subagent" ? "presets" : "workflows";
      return { ...current, agents: { ...current.agents, [key]: [...new Set([...current.agents[key], ...added])] } };
    });
    setPickerTarget(null);
  };

  const handleSave = async (applyToRuntime: boolean) => {
    if (!draft || busy) return;
    const submitted = draft;
    const requestScope = scope.current;
    setSaving(true); setSaveError(null);
    try {
      const result = await api.updateBotTools(instanceId, submitted, { apply: applyToRuntime && isDeployed });
      if (scope.current !== requestScope) return;
      if ("warnings" in result && result.warnings?.length) Message.warning(result.warnings.join("；"));
      if ("id" in result) {
        setApply({ id: result.id, submitted });
        Message.info("应用任务已启动，等待配置写入和重启结果。");
        onApplyTask?.(result, () => { if (scope.current === requestScope) void invalidate(); });
      } else {
        setUnappliedSave(true);
        await refreshSaved(submitted, requestScope);
        if (scope.current === requestScope) Message.success(isDeployed ? "配置已保存，等待应用到服务。" : "配置已保存，部署后生效。");
      }
    } catch (cause) {
      if (scope.current === requestScope) {
        setSaveError(`保存失败：${cause instanceof Error ? cause.message : String(cause)}。修改已保留。`);
        void refetch();
      }
    } finally {
      if (scope.current === requestScope) setSaving(false);
    }
  };

  const pickerItems = pickerTarget ? catalogByKind[pickerTarget] ?? [] : [];
  const pickerSelected = new Set(pickerTarget === "tool_pack" ? draft?.tools.packs : pickerTarget === "tool_feature" ? draft?.tools.features :
    pickerTarget === "mcp" ? draft?.tools.mcp.servers.map((item) => mcpCatalogByRef.get(item.ref)?.name ?? item.ref) :
      pickerTarget === "subagent" ? draft?.agents.presets : draft?.agents.workflows);
  return { error, catalogError: catalog.error, catalogLoading: catalog.isLoading, catalogByKind, dirty, draft, discard, handlePickerConfirm, handleSave, isLoading, unappliedSave, acknowledgeApplied,
    pickerItems, pickerSelected, pickerTarget, removeAgentPreset, removeFeature, removeHiddenTool, removeMcp, removeToolPack, removeWorkflow,
    saving: busy, applying: !!apply, saveError: saveError ?? (applyQuery.isError && apply ? "应用任务状态读取失败，正在重试。" : null), setPickerTarget, toggleMcp };
}
