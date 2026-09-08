import { useEffect, useRef, useState, type ReactNode } from "react";
import { Alert, Button, Empty, Input, Select, Spin, Tag } from "@arco-design/web-react";
import type { Inspection, InspectionEntity } from "./workbenchModel";
import { dateTime } from "./workbenchModel";
import { ConfigFields, FIELD_NAMES } from "./ObservationContent";
import ConfigurationFields from "./ConfigurationFields";
import { CAPABILITY_GROUPS, CONFIGURATION_GROUPS, type ConfigurationTab, type DisplayEntity } from "./configurationPresentation";

export function EntityState({ entity }: { entity: InspectionEntity }) {
  return <span className="obs-entity-states">
    <Tag size="small" color={entity.configured ? "blue" : "gray"}>{entity.configured == null ? "运行时发现" : entity.configured ? "已配置" : "未启用"}</Tag>
    {entity.runtime_stale && <Tag size="small" color="gray">运行快照已过期</Tag>}
    {entity.loaded != null && <Tag size="small" color={entity.loaded ? "green" : "orange"}>{entity.loaded ? "已加载" : "未加载"}</Tag>}
    {!entity.id.startsWith("skill:") && entity.connected != null && <Tag size="small" color={entity.connected ? "green" : "red"}>{entity.connected ? "已连接" : "未连接"}</Tag>}
  </span>;
}

export default function ConfigurationPane({ inspection, entities, loading, error, selectedEntity, revealVersion, view, onRefresh, groupActions, entityActions, groupFooter }: {
  inspection?: Inspection; entities: DisplayEntity[]; loading: boolean; error: Error | null; selectedEntity: string;
  revealVersion: number; view: ConfigurationTab; onRefresh: () => void;
  groupActions: (group: string) => ReactNode; entityActions: (entity: DisplayEntity) => ReactNode;
  groupFooter: (group: string) => ReactNode;
}) {
  const [search, setSearch] = useState("");
  const [category, setCategory] = useState("");
  const pane = useRef<HTMLElement>(null);
  const groups = view === "configuration" ? CONFIGURATION_GROUPS : CAPABILITY_GROUPS;
  const selected = entities.some((item) => item.id === selectedEntity && groups.some(([id]) => id === item.group));
  useEffect(() => { setSearch(""); setCategory(""); }, [view, selectedEntity, revealVersion]);
  useEffect(() => {
    if (!selected || search || category) return;
    const target = Array.from(pane.current?.querySelectorAll<HTMLElement>("[data-entity-id]") ?? []).find((element) => element.dataset.entityId === selectedEntity);
    const frame = requestAnimationFrame(() => target?.scrollIntoView({ block: "start" }));
    return () => cancelAnimationFrame(frame);
  }, [selectedEntity, selected, view, revealVersion, search, category]);
  const status = inspection?.configuration_status ?? (inspection?.loaded_stale || !inspection?.loaded ? "unknown" : inspection?.pending_changes ? "pending" : "unknown");
  return <section ref={pane} className="obs-configuration" aria-label={view === "configuration" ? "分层配置" : "能力与工具"}>
    <div className="obs-config-toolbar"><div className="obs-pane-heading"><strong>当前配置</strong>
      <Tag color={status === "applied" ? "green" : status === "pending" ? "orange" : "gray"}>{status === "applied" ? "已应用" : status === "pending" ? "待应用" : "暂无法确认"}</Tag>
      <Button size="small" onClick={onRefresh}>刷新</Button></div>
      <div className="obs-config-version"><span>版本 {inspection?.current?.configuration_revision?.slice(0, 10) ?? "未记录"}</span>
        {inspection && <span>更新于 {dateTime(inspection.generated_at)}</span>}</div></div>
    {status === "pending" && <Alert type="warning" content="最新配置尚未应用到服务。" />}
    {inspection && status === "unknown" && <Alert type="info" content={inspection.configuration_status_reason || "运行状态暂未更新，配置按当前保存的设置展示。"} />}
    {!!inspection?.errors.length && <Alert type="warning" content={inspection.errors.map((item) => item.message).join("；")} />}
    {inspection?.sanitization_truncated && <Alert type="warning" content="配置响应已截断，当前仅显示已取得的字段。" />}
    {error && <Alert type="error" content={"配置读取失败：" + error.message} />}
    {loading ? <Spin /> : !inspection?.current ? <Empty description="未取得当前配置" /> : <>
      {!!inspection.current.validation?.length && <Alert type="warning" content={inspection.current.validation.map((item) => item.field + "：" + item.message).join("；")} />}
      {selectedEntity && !selected && <Alert type="info" content={"未记录此组件的配置：" + selectedEntity} />}
      <div className="obs-config-search"><Input aria-label="搜索配置组件" placeholder="搜索配置、模型或组件" allowClear value={search} onChange={setSearch} />
        {view === "capabilities" && <Select aria-label="能力分类" value={category} onChange={setCategory} options={[{ value: "", label: "全部分类" }, ...groups.map(([value, label]) => ({ value, label }))]} />}</div>
      <nav className="obs-config-anchors" aria-label="配置分组">{groups.map(([id, name]) =>
        <button type="button" className="obs-link" key={id} onClick={() => {
          setSearch(""); setCategory(""); requestAnimationFrame(() => Array.from(pane.current?.querySelectorAll<HTMLElement>("[data-layer-id]") ?? [])
            .find((element) => element.dataset.layerId === id)?.scrollIntoView({ block: "start" }));
        }}>{name}</button>)}</nav>
      <div className="obs-layer-list">{groups.map(([id, name]) => {
        if (category && category !== id) return null;
        const matching = entities.filter((entity) => entity.group === id &&
          (entity.name + " " + entity.id + " " + JSON.stringify(entity.config) + " " + JSON.stringify(entity.environment)).toLowerCase().includes(search.toLowerCase()));
        if (search && !matching.length) return null;
        return <section key={id} data-layer-id={id} className="obs-layer">
          <div className="obs-pane-heading"><h3>{name}</h3>{groupActions(id)}</div>
          {!matching.length && <p className="obs-muted">未配置</p>}
          {matching.map((entity) => <article key={entity.viewKey} data-entity-id={entity.id} className={"obs-entity" + (selectedEntity === entity.id ? " is-selected" : "")}>
            <div className="obs-pane-heading"><strong>{FIELD_NAMES[entity.name] ?? entity.name}</strong>{entityActions(entity)}</div>
            <EntityState entity={entity} />
            <ConfigurationFields entity={entity} />
            {entity.runtime && <><h4>运行信息</h4><ConfigFields value={entity.runtime} /></>}
          </article>)}{groupFooter(id)}
        </section>;
      })}</div>
    </>}
  </section>;
}
