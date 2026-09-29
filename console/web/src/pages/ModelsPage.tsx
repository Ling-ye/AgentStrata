import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Card, Input, InputNumber, Message, Select, Space, Spin, Typography } from "@arco-design/web-react";
import PageSection from "../shared/ui/PageSection";
import { llmApi, profileOptions, type ModelCatalog, type ModelConfiguration, type ModelConnection } from "../features/llm/api";

const grid = { display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(min(100%,240px),1fr))", gap: 16 } as const;
const { Text } = Typography;

export default function ModelsPage() {
  const client = useQueryClient();
  const query = useQuery({ queryKey: ["llm-config"], queryFn: ({ signal }) => llmApi.config(signal), retry: false });
  const [draft, setDraft] = useState<ModelConfiguration>();
  const [connectionId, setConnectionId] = useState("");
  const [newConnection, setNewConnection] = useState("");
  const [newProfile, setNewProfile] = useState("");
  const [newPurpose, setNewPurpose] = useState("");
  const [catalog, setCatalog] = useState<ModelCatalog>();
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [dirty, setDirty] = useState(false);
  const generation = useRef(0);
  useEffect(() => {
    if (!draft && query.data) {
      setDraft(structuredClone(query.data));
      setConnectionId(Object.keys(query.data.connections)[0] ?? "");
    }
  }, [query.data, draft]);
  async function refresh(id: string, connection?: ModelConnection) {
    const version = ++generation.current;
    if (!id || !connection) { setCatalog(undefined); setLoading(false); return; }
    setLoading(true); setError(""); setCatalog(undefined);
    try { const result = await llmApi.refresh(id, connection); if (version === generation.current) setCatalog(result); }
    catch (value) { if (version === generation.current) setError(String(value)); }
    finally { if (version === generation.current) setLoading(false); }
  }
  useEffect(() => {
    if (connectionId && draft) void refresh(connectionId, draft.connections[connectionId]);
    return () => { generation.current++; };
  // A field edit requires explicit refresh; selecting a connection discovers automatically.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [connectionId]);
  function change(action: (value: ModelConfiguration) => void) {
    if (!draft) return;
    const next = structuredClone(draft); action(next); setDraft(next); setDirty(true);
  }
  function editConnection(patch: Partial<ModelConnection>) {
    change(next => { next.connections[connectionId] = { ...next.connections[connectionId], ...patch }; });
    generation.current++; setCatalog(undefined); setLoading(false);
  }
  async function save() {
    if (!draft) return;
    setSaving(true); setError("");
    try {
      const saved = await llmApi.save(draft);
      setDraft(saved); setDirty(false); client.setQueryData(["llm-config"], saved);
      Message.success("已保存。新任务使用最新方案；常驻机器人请应用配置或重启。");
    } catch (value) { setError(String(value)); }
    finally { setSaving(false); }
  }
  if (!draft) return <PageSection title="模型配置">{query.isError ? <Alert type="error" content={String(query.error)} /> : <Spin />}</PageSection>;
  const connection = draft.connections[connectionId];
  const profiles = Object.entries(draft.profiles).filter(([, value]) => value.connection === connectionId);
  const validId = (id: string) => /^[A-Za-z0-9][A-Za-z0-9_.:-]*$/.test(id);
  return <PageSection title="模型配置" description="集中维护连接、模型方案与用途引用。模型能力只采用当前连接接口明确返回的值。">
    <Space direction="vertical" size={20} style={{ width: "100%" }}>
      <Space wrap><Button type="primary" loading={saving} disabled={!dirty} onClick={() => void save()}>保存配置</Button>
        <Text type="secondary">{dirty ? "有未保存更改" : "已保存"}；机器人通过现有应用／重启入口加载配置。</Text></Space>
      {error && <Alert type="error" content={error} />}
      <Card title="连接">
        <Space direction="vertical" style={{ width: "100%" }}>
          <div style={grid}><Select aria-label="模型连接" value={connectionId || undefined} options={Object.keys(draft.connections)} onChange={setConnectionId} placeholder="选择连接" />
            <Space><Input aria-label="新连接名称" value={newConnection} onChange={setNewConnection} placeholder="新连接标识" />
              <Button disabled={!validId(newConnection) || !!draft.connections[newConnection]} onClick={() => {
                const id = newConnection; change(next => { next.connections[id] = { kind: "codex", auth: { mode: "chatgpt", profile: "main" } }; });
                setConnectionId(id); setNewConnection("");
              }}>新增</Button></Space></div>
          {connection && <>
            <div style={grid}>
              <label>连接类型<Select value={connection.kind} onChange={kind => editConnection({ kind, base_url: undefined,
                auth: kind === "codex" ? { mode: "chatgpt", profile: "main" } : { mode: "api_key", key_env: "OPENAI_API_KEY" } })}
                options={[{ value: "codex", label: "Codex" }, { value: "openai_responses", label: "OpenAI Responses" }, { value: "openai_compatible", label: "OpenAI 兼容 API" }]} /></label>
              <label>请求超时（秒）<InputNumber min={1} value={connection.timeout ?? 120} onChange={timeout => editConnection({ timeout })} /></label>
              {connection.kind !== "codex" && <label>API 地址<Input value={connection.base_url ?? "https://api.openai.com/v1"} onChange={base_url => editConnection({ base_url })} /></label>}
              {connection.kind === "codex" && <label>认证通道<Select value={connection.auth.mode === "chatgpt" ? connection.auth.profile : "api_key"}
                options={[{ value: "main", label: "主模型订阅凭据" }, { value: "worker", label: "Worker 订阅凭据" }, { value: "api_key", label: "API Key" }]}
                onChange={value => editConnection({ auth: value === "api_key" ? { mode: "api_key", key_env: "OPENAI_API_KEY" } : { mode: "chatgpt", profile: value } })} /></label>}
              {connection.auth.mode === "api_key" && <label>API Key 环境变量名<Input value={connection.auth.key_env} onChange={key_env => editConnection({ auth: { mode: "api_key", key_env } })} /></label>}
              <label>私有凭据环境文件（可选）<Input value={connection.env_file ?? ""} placeholder="绝对路径；不在此输入秘密" onChange={env_file => editConnection({ env_file: env_file || undefined })} /></label>
              {connection.kind === "codex" && <><label>Codex 可执行文件环境变量<Input value={connection.codex_bin_env ?? "CHATCOPILOT_CODEX_BIN"} onChange={codex_bin_env => editConnection({ codex_bin_env })} /></label>
                <label>凭据根目录环境变量<Input value={connection.credential_root_env ?? "CHATCOPILOT_CODEX_BOT_HOME"} onChange={credential_root_env => editConnection({ credential_root_env })} /></label></>}
            </div>
            <Space wrap><Button loading={loading} onClick={() => void refresh(connectionId, connection)}>刷新模型目录</Button>
              <Button status="danger" disabled={profiles.length > 0} onClick={() => { change(next => { delete next.connections[connectionId]; }); setConnectionId(""); }}>删除连接</Button>
              <Text type="secondary">被方案引用的连接需先解除引用。</Text></Space>
            {catalog?.error && <Alert type="warning" content={`${catalog.error}。${catalog.fetched_at ? "以下为上次成功结果。" : "尚无可用目录。"}`} />}
            {catalog?.fetched_at && <Text type="secondary">来源：{catalog.source} · 获取时间：{new Date(catalog.fetched_at).toLocaleString()}</Text>}
          </>}
        </Space>
      </Card>
      <Card title="当前连接的模型方案">
        <Space direction="vertical" size={16} style={{ width: "100%" }}>
          {profiles.map(([id, profile]) => {
            const model = catalog?.models.find(item => item.id === profile.model);
            return <div key={id} style={grid}>
              <Text bold>{id}</Text>
              <Select aria-label={`${id} 模型`} showSearch value={profile.model || undefined} loading={loading} disabled={!catalog?.fetched_at || !!catalog.error}
                placeholder="选择发现的模型" options={(catalog?.models ?? []).filter(item => !item.hidden || item.id === profile.model).map(item => ({ value: item.id, label: item.name }))}
                onChange={value => change(next => {
                  const selected = catalog?.models.find(item => item.id === value);
                  next.profiles[id] = { connection: connectionId, model: value,
                    ...(selected?.default_reasoning_effort && selected.reasoning_efforts?.includes(selected.default_reasoning_effort) ? { reasoning_effort: selected.default_reasoning_effort } : {}) };
                })} />
              <Select aria-label={`${id} 推理强度`} value={profile.reasoning_effort} allowClear disabled={!model?.reasoning_efforts?.length || !!catalog?.error}
                options={model?.reasoning_efforts ?? []} placeholder={model?.reasoning_efforts ? "使用服务商默认值" : "接口未提供推理选项"}
                onChange={value => change(next => { if (value) next.profiles[id].reasoning_effort = value; else delete next.profiles[id].reasoning_effort; })} />
              <Text type="secondary">上下文：{model?.context_window?.toLocaleString() ?? "接口未提供"}；只读</Text>
              {catalog?.fetched_at && !model && <Text type="warning">已保存模型未出现在目录中，请重新选择。</Text>}
              <Button size="small" status="danger" disabled={Object.values(draft.bindings).includes(id)} onClick={() => change(next => { delete next.profiles[id]; })}>删除方案</Button>
            </div>;
          })}
          <Space><Input aria-label="新方案名称" value={newProfile} onChange={setNewProfile} placeholder="新方案标识" />
            <Button disabled={!connection || !validId(newProfile) || !!draft.profiles[newProfile]} onClick={() => {
              change(next => { next.profiles[newProfile] = { connection: connectionId, model: "" }; }); setNewProfile("");
            }}>新增方案</Button></Space>
        </Space>
      </Card>
      <Card title="用途引用">
        <Space direction="vertical" style={{ width: "100%" }}>
          <Text type="secondary">机器人使用 BotSpec 中的 binding；后台用途为 harness、code_health、evaluation.judge。多个用途可引用同一方案。</Text>
          {Object.entries(draft.bindings).map(([purpose, profile]) => <div key={purpose} style={grid}>
            <Text>{purpose}</Text><Select aria-label={`${purpose} 方案`} value={profile} options={profileOptions(draft)} onChange={value => change(next => { next.bindings[purpose] = value; })} />
            <Button size="small" status="danger" onClick={() => change(next => { delete next.bindings[purpose]; })}>解除引用</Button>
          </div>)}
          <Space wrap><Input aria-label="新用途名称" value={newPurpose} onChange={setNewPurpose} placeholder="用途标识，如 harness" />
            <Select placeholder="选择方案并添加用途" value={undefined} disabled={!validId(newPurpose) || newPurpose in draft.bindings}
              options={profileOptions(draft)} onChange={profile => { change(next => { next.bindings[newPurpose] = profile; }); setNewPurpose(""); }} style={{ minWidth: 240 }} /></Space>
        </Space>
      </Card>
    </Space>
  </PageSection>;
}
