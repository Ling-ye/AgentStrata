import { useDeferredValue, useMemo, useState } from "react";
import { Button, Input, Message, Radio } from "@arco-design/web-react";
import { useDetailValue } from "./observationDetailState";
import { copyDataText, pointerChild, searchData, valueSummary, valueType } from "./structuredDataModel";
import "../../styles/structured-data.css";

type Labels = Record<string, string>;
async function copy(value: string, label: string) {
  try { await copyDataText(value); Message.success(`${label}已复制`); }
  catch { Message.error("复制失败，请选择文本手动复制"); }
}
function JsonText({ json }: { json: string }) {
  const [full, setFull] = useDetailValue<boolean>("json-full", false);
  return <>
    <pre className="structured-json">{full ? json : json.slice(0, 8000)}</pre>
    {json.length > 8000 && <Button size="mini" onClick={() => setFull(!full)}>{full ? "收起 JSON" : `展开完整 JSON（${json.length.toLocaleString()} 字符）`}</Button>}
  </>;
}
export function JsonData({ value }: { value: unknown }) {
  const json = useMemo(() => JSON.stringify(value, null, 2), [value]);
  if (json === undefined) return <p className="obs-muted">未记录</p>;
  return <div className="structured-json-view">
    <div className="structured-toolbar"><Button size="mini" type="text" onClick={() => void copy(json, "JSON")}>复制 JSON</Button></div>
    <JsonText json={json} />
  </div>;
}
function TreeNode({ value, path, name, labels, root = false }: {
  value: unknown; path: string; name: string; labels: Labels; root?: boolean;
}) {
  const [open, setOpen] = useDetailValue<boolean>(`tree:${path}`, root);
  const [limit, setLimit] = useDetailValue<number>(`items:${path}`, 40);
  const [full, setFull] = useDetailValue<boolean>(`text:${path}`, false);
  const container = value !== null && typeof value === "object";
  const entries = container ? Object.entries(value) : [];
  const summary = valueSummary(value), long = typeof value === "string" && summary.length > 600;
  return <div className="structured-node" data-json-path={path}>
    <div className="structured-row">
      {entries.length > 0 ? <button type="button" className="structured-toggle" aria-label={`${open ? "收起" : "展开"} ${path || "根"}`}
        aria-expanded={open} onClick={() => setOpen(!open)}>{open ? "▾" : "▸"}</button> : <span className="structured-toggle" />}
      <code className="structured-key" title={path || "根对象"}>{name}</code>
      {labels[name] && <span className="structured-label">{labels[name]}</span>}
      <span className="structured-type">{valueType(value)}</span>
      <span className={"structured-value is-" + valueType(value)}>{long && !full ? `${summary.slice(0, 600)}…` : summary}</span>
    </div>
    {long && <Button type="text" size="mini" onClick={() => setFull(!full)}>{full ? "收起正文" : `展开全文（${summary.length.toLocaleString()} 字符）`}</Button>}
    {open && entries.length > 0 && <div className="structured-children">{entries.slice(0, limit).map(([key, item]) =>
      <TreeNode key={key} value={item} path={pointerChild(path, key)} name={key} labels={labels} />)}
      {entries.length > limit && <Button size="mini" onClick={() => setLimit(limit + 40)}>显示更多（剩余 {entries.length - limit} 项）</Button>}
    </div>}
  </div>;
}
export default function StructuredData({ value, labels = {}, missingLabel = "字段缺失" }: {
  value: unknown; labels?: Labels; missingLabel?: string;
}) {
  const [mode, setMode] = useDetailValue<number>("data-mode", 0);
  const [query, setQuery] = useState("");
  const deferred = useDeferredValue(query);
  const matches = useMemo(() => searchData(value, deferred), [value, deferred]);
  const json = useMemo(() => mode === 2 ? JSON.stringify(value, null, 2) : undefined, [mode, value]);
  const showingJson = mode === 2 && !deferred.trim();
  if (value === undefined) return <p className="obs-muted">{missingLabel} · missing</p>;
  const controls = <><Radio.Group type="button" size="mini" value={mode === 2 ? 2 : 0} onChange={setMode}
      options={[{ label: "树", value: 0 }, { label: "JSON", value: 2 }]} />
      <Input.Search size="mini" allowClear aria-label="搜索已加载数据" placeholder="搜索已加载的键和值" value={query} onChange={setQuery} />
      {showingJson && <Button size="mini" type="text" onClick={() => void copy(json!, "JSON")}>复制 JSON</Button>}
    </>;
  return <div className="structured-data">
    <div className="structured-toolbar">{controls}</div>
    {deferred.trim() ? <div className="structured-search"><p className="obs-muted">当前已加载载荷 · {matches.more ? "前 " : ""}{matches.matches.length} 个匹配{matches.more ? "（请缩小搜索范围）" : ""}</p>
      {matches.matches.map((match) => <div key={match.path}><code className="structured-match-path">{match.path || "根"}</code>
        <TreeNode {...match} name={match.key} labels={labels} /></div>)}
    </div> : showingJson ? <JsonText json={json!} /> :
      <TreeNode value={value} path="" name="$" labels={labels} root />}
  </div>;
}
