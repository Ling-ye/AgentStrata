import { useEffect, useRef, useState } from "react";
import { Alert, Button, Card, Input, Select, Space, Spin, Typography } from "@arco-design/web-react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { managementRequest, settingsChanged, type UserSettings } from "./settingsApi";
const { Text, Paragraph } = Typography;

export default function ApplicationSettings({ id, onDirty }: { id: string; onDirty: (dirty: boolean) => void }) {
  const query = useQuery({ queryKey: ["application-user-settings", id], queryFn: ({ signal }) => managementRequest<UserSettings>(id, "/settings", { signal }), retry: false });
  const [draft, setDraft] = useState<UserSettings>();
  const [busy, setBusy] = useState("");
  const [review, setReview] = useState("");
  const [error, setError] = useState("");
  const alive = useRef(true); const gate = useRef(false); const client = useQueryClient();
  const dirty = !!draft && !!query.data && (["model", "prompts"] as const).some(section => settingsChanged(query.data[section].values, draft[section].values));
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  useEffect(() => { onDirty(dirty || !!busy); }, [dirty, busy, onDirty]);
  useEffect(() => { if (query.data && !dirty && !busy) setDraft(structuredClone(query.data)); }, [query.data, dirty, busy]);
  async function save(section: "model" | "prompts") {
    if (!draft || gate.current) return;
    gate.current = true; setBusy(section); setError("");
    try {
      await managementRequest(id, "/settings/" + section, { method: "POST", body: JSON.stringify({ revision: draft[section].revision, values: draft[section].values }) });
      const saved = await managementRequest<UserSettings>(id, "/settings");
      if (alive.current) {
        setDraft(current => current && ({ ...current, [section]: saved[section], models: saved.models }));
        client.setQueryData(["application-user-settings", id], saved); setReview("");
      }
    } catch (e) { if (alive.current) setError(e instanceof Error ? e.message : "保存失败，请重新核对配置。"); }
    finally { gate.current = false; if (alive.current) setBusy(""); }
  }
  if (!draft || !query.data) return query.isError ? <Alert type="error" content={query.error.message} action={<Button onClick={() => void query.refetch()}>重试</Button>} /> : <Spin />;
  const change = (section: "model" | "prompts", key: string, value: string) => {
    setReview(""); setDraft({ ...draft, [section]: { ...draft[section], values: { ...draft[section].values, [key]: value } } });
  };
  return <Space direction="vertical" size={16} style={{ width: "100%" }}>
    <Text type="secondary">保存不会中断当前对话。重新启动应用后加载新的模型与提示词。</Text>
    {error && <Alert type="error" content={error} action={<Button disabled={!!busy} onClick={async () => { const result = await query.refetch(); if (result.data) { setDraft(structuredClone(result.data)); setError(""); setReview(""); } }}>重新读取并放弃草稿</Button>} />}
    {(["model", "prompts"] as const).map(section => {
      const saved = query.data[section]; const value = draft[section]; const changed = settingsChanged(saved.values, value.values);
      return <Card key={section} title={section === "model" ? "模型" : "人设与回复风格"} extra={<Text type="secondary">{changed ? "尚未保存" : { applied: "已加载", pending: "已保存，待重启生效", unknown: "已保存，尚未核对加载状态" }[saved.status]}</Text>}>
        <Space direction="vertical" size={16} style={{ width: "100%" }}>
          {section === "model" ? <><Select aria-label="应用模型" value={value.values.profile || undefined} disabled={!!busy} placeholder="选择已配置的模型方案" onChange={v => change(section, "profile", v)} options={draft.models.map(m => ({ value: m.id, label: `${m.model} · ${m.connection}` }))} /><Button href="#models">管理模型连接</Button></> : <>
            <label>人设<Input.TextArea aria-label="应用人设" value={value.values.identity} disabled={!!busy} autoSize={{ minRows: 4, maxRows: 12 }} onChange={v => change(section, "identity", v)} /></label>
            <label>回复风格<Input.TextArea aria-label="回复风格" value={value.values.response_style} disabled={!!busy} autoSize={{ minRows: 3, maxRows: 8 }} onChange={v => change(section, "response_style", v)} /></label>
          </>}
          {review === section && <Card title="确认修改">{Object.keys(value.values).filter(key => value.values[key] !== saved.values[key]).map(key => <div key={key}><Text bold>{key === "identity" ? "人设" : key === "response_style" ? "回复风格" : "模型方案"}</Text><Paragraph className="application-prose">原值：{saved.values[key] || "未配置"}</Paragraph><Paragraph className="application-prose">新值：{value.values[key]}</Paragraph></div>)}</Card>}
          <Space><Button type="primary" disabled={!changed || !!busy || Object.values(value.values).some(v => !v.trim() || v.length > 16000)} loading={busy === section} onClick={() => review === section ? void save(section) : setReview(section)}>{review === section ? "确认保存" : "查看修改"}</Button>
            <Button disabled={changed || !!busy} onClick={() => void query.refetch()}>核对生效状态</Button></Space>
        </Space>
      </Card>;
    })}
  </Space>;
}
