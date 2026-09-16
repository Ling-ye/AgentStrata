import { useDeferredValue, useMemo, useState } from "react";
import { Button, Input, Message, Radio } from "@arco-design/web-react";
import { useDetailValue } from "./observationDetailState";
import { copyDataText, isObjectArray, pointerChild, searchData, valueSummary, valueType } from "./structuredDataModel";
import "../../styles/structured-data.css";

type Labels = Record<string, string>;
async function copy(value: string, label: string) {
  try { await copyDataText(value); Message.success(`${label}已复制`); }
  catch { Message.error("复制失败，请选择文本手动复制"); }
}
function Actions({ value, path }: { value: unknown; path: string }) {
  return <span className="structured-actions">
    <Button size="mini" type="text" aria-label={`复制字段路径 ${path || "根"}`} title="复制相对当前载荷的 JSON Pointer（原始键）"
      onClick={() => void copy(path, "字段路径")}>路径</Button>
    <Button size="mini" type="text" disabled={value === undefined} aria-label={`复制值 ${path || "根"}`}
      onClick={() => void copy(JSON.stringify(value, null, 2), "值")}>复制</Button>
  </span>;
}
function TreeNode({ value, path, name, labels, root = false }: {
  value: unknown; path: string; name: string; labels: Labels; root?: boolean;
}) {
  const [open, setOpen] = useDetailValue<boolean>(`tree:${path}`, root);
  const [limit, setLimit] = useDetailValue<number>(`items:${path}`, 40);
  const [full, setFull] = useDetailValue<boolean>(`text:${path}`, false);
  const [asTable, setAsTable] = useDetailValue<boolean>(`table:${path}`, false);
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
      {!root && isObjectArray(value) && <Button type="text" size="mini" onClick={() => { setAsTable(!asTable); setOpen(true); }}>{asTable ? "树" : "表格"}</Button>}
      <Actions value={value} path={path} />
    </div>
    {long && <Button type="text" size="mini" onClick={() => setFull(!full)}>{full ? "收起正文" : `展开全文（${summary.length.toLocaleString()} 字符）`}</Button>}
    {open && asTable && isObjectArray(value) ? <DataTable value={value} labels={labels} path={path} /> : open && entries.length > 0 && <div className="structured-children">{entries.slice(0, limit).map(([key, item]) =>
      <TreeNode key={key} value={item} path={pointerChild(path, key)} name={key} labels={labels} />)}
      {entries.length > limit && <Button size="mini" onClick={() => setLimit(limit + 40)}>显示更多（剩余 {entries.length - limit} 项）</Button>}
    </div>}
  </div>;
}
function DataTable({ value, labels, path = "" }: { value: Record<string, unknown>[]; labels: Labels; path?: string }) {
  const [limit, setLimit] = useDetailValue<number>(`table:${path}:rows`, 40);
  const [columnLimit, setColumnLimit] = useDetailValue<number>(`table:${path}:columns`, 12);
  const columns = useMemo(() => [...new Set(value.flatMap(Object.keys))], [value]);
  return <><div className="structured-table-scroll"><table className="structured-table"><thead><tr><th scope="col">索引</th>
    {columns.slice(0, columnLimit).map((key) => <th scope="col" key={key}><code>{key}</code>{labels[key] && <small>{labels[key]}</small>}</th>)}
  </tr></thead><tbody>{value.slice(0, limit).map((row, index) => <tr key={index}><th scope="row">{index}</th>
    {columns.slice(0, columnLimit).map((key) => <td key={key}><TreeNode value={Object.prototype.hasOwnProperty.call(row, key) ? row[key] : undefined}
      path={pointerChild(pointerChild(path, index), key)} name={key} labels={{}} /></td>)}
  </tr>)}</tbody></table></div>
    {columns.length > columnLimit && <Button size="mini" onClick={() => setColumnLimit(columnLimit + 12)}>显示更多列（剩余 {columns.length - columnLimit}）</Button>}
    {value.length > limit && <Button size="mini" onClick={() => setLimit(limit + 40)}>显示更多行（剩余 {value.length - limit}）</Button>}
  </>;
}
export default function StructuredData({ value, labels = {}, missingLabel = "字段缺失" }: {
  value: unknown; labels?: Labels; missingLabel?: string;
}) {
  const table = isObjectArray(value);
  const [mode, setMode] = useDetailValue<number>("data-mode", table ? 1 : 0);
  const [query, setQuery] = useState("");
  const deferred = useDeferredValue(query);
  const matches = useMemo(() => searchData(value, deferred), [value, deferred]);
  const [full, setFull] = useDetailValue<boolean>("json-full", false);
  const json = useMemo(() => mode === 2 ? JSON.stringify(value, null, 2) ?? missingLabel : "", [value, mode, missingLabel]);
  if (value === undefined) return <p className="obs-muted">{missingLabel} · missing</p>;
  return <div className="structured-data">
    <div className="structured-toolbar"><Radio.Group type="button" size="mini" value={mode === 1 && !table ? 0 : mode} onChange={setMode}
      options={[{ label: "树", value: 0 }, ...(table ? [{ label: "表格", value: 1 }] : []), { label: "JSON", value: 2 }]} />
      <Input.Search size="mini" allowClear aria-label="搜索已加载数据" placeholder="搜索已加载的键和值" value={query} onChange={setQuery} />
      <Button size="mini" type="text" onClick={() => void copy(JSON.stringify(value, null, 2), "当前载荷")}>复制全部</Button>
    </div>
    {deferred.trim() ? <div className="structured-search"><p className="obs-muted">当前已加载载荷 · {matches.more ? "前 " : ""}{matches.matches.length} 个匹配{matches.more ? "（请缩小搜索范围）" : ""}</p>
      {matches.matches.map((match) => <div key={match.path}><code className="structured-match-path">{match.path || "根"}</code>
        <TreeNode {...match} name={match.key} labels={labels} /></div>)}
    </div> : mode === 2 ? <><pre className="structured-json">{full ? json : json.slice(0, 8000)}</pre>
      {json.length > 8000 && <Button size="mini" onClick={() => setFull(!full)}>{full ? "收起 JSON" : `展开完整 JSON（${json.length.toLocaleString()} 字符）`}</Button>}</> :
      mode === 1 && table ? <DataTable value={value} labels={labels} /> : <TreeNode value={value} path="" name="$" labels={labels} root />}
  </div>;
}
