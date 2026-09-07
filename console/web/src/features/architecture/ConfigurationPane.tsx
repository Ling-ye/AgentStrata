import { useEffect, useRef, useState } from "react";
import { Alert, Button, Empty, Input, Spin, Tag } from "@arco-design/web-react";
import type { Inspection, InspectionEntity } from "./workbenchModel";
import { dateTime } from "./workbenchModel";
import { ConfigFields, FIELD_NAMES } from "./ObservationContent";
import { latestConfiguration } from "./configurationModel";

function EntityState({ entity }: { entity: InspectionEntity }) {
  return <span className="obs-entity-states">
    <Tag size="small" color={entity.configured ? "blue" : "gray"}>{entity.configured == null ? "配置未记录" : entity.configured ? "已配置" : "未启用"}</Tag>
    {entity.loaded != null && <Tag size="small" color={entity.loaded ? "green" : "orange"}>{entity.loaded ? "已加载" : "未加载"}</Tag>}
    {entity.connected != null && <Tag size="small" color={entity.connected ? "green" : "red"}>{entity.connected ? "已连接" : "未连接"}</Tag>}
    {entity.available != null && <Tag size="small" color={entity.available ? "green" : "gray"}>{entity.available ? "本次可用" : "本次不可用"}</Tag>}
  </span>;
}

export default function ConfigurationPane({ inspection, loading, error, selectedEntity, revealVersion, onEdit }: {
  inspection?: Inspection; loading: boolean; error: Error | null; selectedEntity: string;
  revealVersion: number; onEdit: () => void;
}) {
  const [search, setSearch] = useState("");
  const pane = useRef<HTMLElement>(null);
  const config = latestConfiguration(inspection);
  const selectedLayer = config?.entities.find((item) => item.id === selectedEntity)?.layer;
  useEffect(() => {
    if (!selectedEntity || !selectedLayer) return;
    const target = Array.from(pane.current?.querySelectorAll<HTMLElement>("[data-entity-id]") ?? [])
      .find((element) => element.dataset.entityId === selectedEntity);
    const frame = requestAnimationFrame(() => {
      target?.scrollIntoView({ block: "start" });
    });
    return () => cancelAnimationFrame(frame);
  }, [selectedEntity, selectedLayer, revealVersion]);
  return <section ref={pane} className="obs-configuration" aria-label="分层配置">
    <div className="obs-config-toolbar"><div className="obs-pane-heading"><strong>当前配置</strong>
      <Button size="small" onClick={onEdit}>编辑配置</Button></div>
      <div className="obs-config-version"><span>版本 {config?.configuration_revision?.slice(0, 10) ?? "未记录"}</span>
        {inspection && <span>更新于 {dateTime(inspection.generated_at)}</span>}</div></div>
    {inspection?.pending_changes && <Alert type="warning" content="最新配置尚未应用到服务。" />}
    {inspection && (!inspection.loaded || inspection.loaded_stale) && <Alert type="info" content="运行状态暂未更新，配置内容已按当前保存的设置展示。" />}
    {!!inspection?.errors.length && <Alert type="warning" content={inspection.errors.map((item) => item.message).join("；")} />}
    {inspection?.sanitization_truncated && <Alert type="warning" content="配置响应已截断，当前仅显示已取得的字段。" />}
    {error && <Alert type="error" content={"配置读取失败：" + error.message} />}
    {loading ? <Spin /> : !config ? <Empty description="未取得当前配置" /> : <>
      {!!config.validation?.length && <Alert type="warning" content={config.validation.map((item) => item.field + "：" + item.message).join("；")} />}
      {selectedEntity && !selectedLayer && <Alert type="info" content={"未记录此组件的配置：" + selectedEntity} />}
      <Input aria-label="搜索配置组件" placeholder="搜索配置、模型或组件" allowClear value={search} onChange={setSearch} />
      <nav className="obs-config-anchors" aria-label="配置分组">{config.layers.map((layer) =>
        <button type="button" className="obs-link" key={layer.id} onClick={() => {
          setSearch(""); requestAnimationFrame(() => Array.from(pane.current?.querySelectorAll<HTMLElement>("[data-layer-id]") ?? [])
            .find((element) => element.dataset.layerId === layer.id)?.scrollIntoView({ block: "start" }));
        }}>{layer.name}</button>)}</nav>
      <div className="obs-layer-list">{config.layers.map((layer) => {
        const entities = config.entities.filter((entity) => entity.layer === layer.id &&
          (entity.name + " " + entity.id + " " + JSON.stringify(entity.config) + " " + JSON.stringify(entity.environment)).toLowerCase().includes(search.toLowerCase()));
        if (search && !entities.length) return null;
        return <section key={layer.id} data-layer-id={layer.id} className="obs-layer">
          <h3>{layer.name}<small>{entities.length}</small></h3>
          {!entities.length && <p className="obs-muted">未配置组件</p>}
          {entities.map((entity) => <article key={entity.id} data-entity-id={entity.id} className={"obs-entity" + (selectedEntity === entity.id ? " is-selected" : "")}>
            <div className="obs-pane-heading"><strong>{FIELD_NAMES[entity.name] ?? entity.name}</strong></div>
            <EntityState entity={entity} />
            <ConfigFields value={entity.config} missingLabel="未设置" />
            {entity.runtime && <><h4>运行信息</h4><ConfigFields value={entity.runtime} /></>}
            {entity.environment && Object.keys(entity.environment).length > 0 && <><h4>环境配置</h4><ConfigFields value={entity.environment} missingLabel="未设置" /></>}
          </article>)}
        </section>;
      })}</div>
    </>}
  </section>;
}
