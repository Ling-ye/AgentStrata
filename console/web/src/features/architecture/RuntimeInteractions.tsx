import { useEffect, useState } from "react";
import { Alert, Button, Checkbox, Input, InputNumber, Select, Space } from "@arco-design/web-react";

type FormSchema = { type?: string; title?: string; description?: string; enum?: Array<string | number>;
  properties?: Record<string, FormSchema>; required?: string[] };
function FormFields({ schema, value, change }: { schema: FormSchema; value: Record<string, unknown>;
  change: (value: Record<string, unknown>) => void }) {
  return <>{Object.entries(schema.properties ?? {}).map(([name, field]) => {
    const label = `${field.title || name}${schema.required?.includes(name) ? " *" : ""}`;
    const set = (next: unknown) => change({ ...value, [name]: next });
    return <div key={name}><label>{label}</label>{field.description && <p>{field.description}</p>}
      {field.type === "boolean" ? <Checkbox checked={value[name] === true} onChange={set}>{label}</Checkbox>
        : field.enum ? <Select value={value[name] as string | number | undefined} onChange={set}
            options={field.enum.map(item => ({ value: item, label: String(item) }))} />
        : field.type === "integer" || field.type === "number" ? <InputNumber value={value[name] as number | undefined} onChange={set} />
        : field.type === "object" ? <FormFields schema={field} value={(value[name] as Record<string, unknown>) || {}} change={set} />
        : <Input value={typeof value[name] === "string" ? value[name] as string : ""} onChange={set} />}
    </div>;
  })}</>;
}

type Pending = {
  interactionId: string; kind: "approval" | "user_input" | "mcp_elicitation";
  state: string; payload: { questions?: Array<{ id: string; question: string; options?: Array<{ label: string }> }>;
    [key: string]: unknown };
};

export function RuntimeInteractions({ instanceId }: { instanceId: string }) {
  const [rows, setRows] = useState<Pending[]>([]);
  const [error, setError] = useState("");
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [forms, setForms] = useState<Record<string, Record<string, unknown>>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const endpoint = `/api/bots/${encodeURIComponent(instanceId)}/interactions`;
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const refresh = async () => {
      try {
        const response = await fetch(endpoint, { signal: controller.signal, cache: "no-store" });
        if (!response.ok) { setError("无法读取交互请求，请检查实例的 Console 操作员凭据。"); return; }
        const result = await response.json();
        setError("");
        setRows(result.interactions.filter((row: Pending) => row.state === "pending"));
      } catch { /* Existing runtime observations remain available when controls are unavailable. */ }
      finally { if (!controller.signal.aborted) timer = setTimeout(refresh, 3000); }
    };
    void refresh();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [endpoint]);
  async function resolve(row: Pending, decision?: string) {
    setBusy(row.interactionId); setError("");
    try {
      const resolution = row.kind === "approval" ? { decision } : row.kind === "user_input"
        ? { answers: Object.fromEntries((row.payload.questions ?? []).map(q => [q.id,
            { answers: [answers[`${row.interactionId}:${q.id}`] ?? ""] }])) }
        : { action: decision || "accept", content: row.payload.requestedSchema ? forms[row.interactionId] || {} : null };
      const response = await fetch(`${endpoint}/${encodeURIComponent(row.interactionId)}/resolve`, {
        method: "POST", headers: { "Content-Type": "application/json", "X-AgentStrata-Action": "interaction" },
        body: JSON.stringify({ resolution }),
      });
      if (!response.ok) throw new Error("答复未确认，请刷新请求状态后重试");
      setRows(current => current.filter(item => item.interactionId !== row.interactionId));
    } catch (reason) { setError(reason instanceof Error ? reason.message : "答复失败"); }
    finally { setBusy(null); }
  }
  return <section aria-label="运行交互">
    {error && <Alert type="error" content={error} />}
    {rows.map(row => <div key={row.interactionId}>
      <p>待处理请求 · {row.interactionId}</p>
      <p>以 Console 操作员身份答复；不改变原用户权限。</p>
      {row.kind === "approval" ? <><pre>{JSON.stringify(row.payload, null, 2)}</pre><Space>
        <Button disabled={busy !== null} onClick={() => void resolve(row, "approve")}>允许本次</Button>
        <Button disabled={busy !== null} onClick={() => void resolve(row, "deny")}>拒绝</Button>
      </Space></> : <>
        {(row.payload.questions ?? []).map(q => <label key={q.id}>{q.question}
          {q.options && <Select placeholder="选择一个选项，或在下方输入答复"
            options={q.options.map(option => ({ label: option.label, value: option.label }))}
            onChange={value => setAnswers(current => ({ ...current, [`${row.interactionId}:${q.id}`]: String(value) }))} />}
          <Input value={answers[`${row.interactionId}:${q.id}`] ?? ""}
            onChange={value => setAnswers(current => ({ ...current, [`${row.interactionId}:${q.id}`]: value }))} />
        </label>)}
        {row.kind === "mcp_elicitation" && <><p>{String(row.payload.message ?? "请完成服务要求的确认")}</p>
          {typeof row.payload.url === "string" && /^https?:\/\//.test(row.payload.url)
            && <a href={row.payload.url} target="_blank" rel="noreferrer">打开服务确认页面</a>}
          {row.payload.requestedSchema != null && <FormFields schema={row.payload.requestedSchema as FormSchema}
            value={forms[row.interactionId] ?? {}} change={value => setForms(current => ({ ...current, [row.interactionId]: value }))} />}
          <Button disabled={busy !== null} onClick={() => void resolve(row, "decline")}>拒绝请求</Button>
        </>}
        <Button disabled={busy !== null} onClick={() => void resolve(row)}>提交答复</Button>
      </>}
    </div>)}
  </section>;
}
