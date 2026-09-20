import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { Alert, Button, Drawer, Empty, Input, Select, Spin, Tag } from "@arco-design/web-react";
import type { Inspection, InspectionEntity } from "./workbenchModel";
import { dateTime } from "./workbenchModel";
import { FIELD_NAMES } from "./ObservationContent";
import ConfigurationFields, { ConfigurationValues } from "./ConfigurationFields";
import { readSessionValue, saveSessionValue } from "./taskWorkspaceState";
import {
  CONFIGURATION_LAYERS, configurationLink, configurationSearch, configurationSelection, configurationSummary,
  type ConfigurationGroup, type ConfigurationLayer, type DisplayEntity,
} from "./configurationPresentation";

export function EntityState({ entity }: { entity: InspectionEntity & { applicable?: boolean } }) {
  if (entity.applicable === false) return <Tag size="small">不适用</Tag>;
  const observedComponent = /^(pack:|mcp:|tool:)/.test(entity.id);
  return <span className="obs-entity-states">
    {entity.configured == null ? <Tag size="small">运行时发现</Tag> : entity.configured === false ?
      <Tag size="small">{entity.id.startsWith("tool:") ? "已隐藏" : "未启用"}</Tag> : null}
    {entity.membership === "custom" && <Tag size="small">自定义</Tag>}
    {observedComponent && !entity.runtime_stale && entity.loaded != null &&
      <Tag size="small" color={entity.loaded ? "green" : "orange"}>{entity.loaded ? entity.id.startsWith("tool:") ? "已注册" : "已加载" : "未加载"}</Tag>}
    {entity.id.startsWith("mcp:") && !entity.runtime_stale && entity.connected != null && <Tag size="small" color={entity.connected ? "green" : "red"}>{entity.connected ? "已连接" : "未连接"}</Tag>}
  </span>;
}

function entityName(entity: DisplayEntity) { return FIELD_NAMES[entity.name] ?? entity.name; }
function entityLocation(entity: DisplayEntity) {
  const layer = CONFIGURATION_LAYERS.find((item) => item.id === entity.displayLayer);
  return layer ? `${layer.name} → ${layer.groups.find(([id]) => id === entity.group)?.[1]}` : "实例信息";
}

export default function ConfigurationPane({ instanceId, visible, infoVisible, onInfoClose, inspection, entities, loading, error, onRefresh, groupActions, entityActions, groupFooter }: {
  instanceId: string; visible: boolean; infoVisible: boolean; onInfoClose: () => void;
  inspection?: Inspection; entities: DisplayEntity[]; loading: boolean; error: Error | null; onRefresh: () => void;
  groupActions: (group: ConfigurationGroup) => ReactNode; entityActions: (entity: DisplayEntity, navigate: (id: string) => void) => ReactNode;
  groupFooter: (group: ConfigurationGroup) => ReactNode;
}) {
  const [search, setSearch] = useState("");
  const [params, setParams] = useState(() => new URLSearchParams(window.location.hash.split("?")[1]));
  const [narrow, setNarrow] = useState(window.innerWidth < 860);
  const pane = useRef<HTMLElement>(null);
  const opener = useRef<HTMLElement | null>(null);
  const infoOpener = useRef<HTMLElement | null>(null);
  const openedKey = useRef("");
  const storageKey = `configuration-layer:${instanceId}`;
  const [savedLayer, setSavedLayer] = useState(() => readSessionValue(storageKey));
  const { layer, selected, missing } = configurationSelection(entities, params, savedLayer);
  const layerDefinition = CONFIGURATION_LAYERS.find((item) => item.id === layer)!;
  const matches = configurationSearch(entities, search);
  const instanceEntities = entities.filter((entity) => entity.displayLayer === null);
  const infoOpen = infoVisible || visible && selected?.displayLayer === null;

  useLayoutEffect(() => {
    const update = () => {
      const next = new URLSearchParams(window.location.hash.split("?")[1]);
      if (window.location.hash.startsWith("#bots?") && next.get("instance") === instanceId && next.get("tab") === "configuration") setParams(next);
    };
    if (visible) update();
    window.addEventListener("hashchange", update);
    return () => window.removeEventListener("hashchange", update);
  }, [instanceId, visible]);
  useLayoutEffect(() => {
    if (infoVisible) infoOpener.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  }, [infoVisible]);
  useEffect(() => {
    if (!visible) return;
    saveSessionValue(storageKey, layer);
    setSavedLayer(layer);
  }, [layer, storageKey, visible]);
  useEffect(() => {
    const element = pane.current?.parentElement;
    if (!element) return;
    const observer = new ResizeObserver(([entry]) => { if (entry.contentRect.width > 0) setNarrow(entry.contentRect.width < 860); });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  const writeParams = (next: URLSearchParams) => {
    next.set("instance", instanceId); next.set("tab", "configuration");
    window.history.replaceState(null, "", "#bots?" + next);
    setParams(next);
  };
  const chooseLayer = (next: ConfigurationLayer) => {
    const location = new URLSearchParams(params);
    location.set("layer", next); location.delete("entity"); location.delete("section");
    setSearch(""); writeParams(location);
  };
  const openEntity = (entity: DisplayEntity, element?: HTMLElement) => {
    if (element) opener.current = element;
    openedKey.current = entity.viewKey;
    setSearch("");
    writeParams(new URLSearchParams(configurationLink(instanceId, entity.id, entity.group).split("?")[1]));
  };
  const navigate = (id: string) => {
    const target = entities.find((entity) => entity.id === id);
    if (target) openEntity(target);
  };
  const closeDetail = () => {
    const next = new URLSearchParams(params);
    next.set("layer", layer); next.delete("entity"); next.delete("section");
    writeParams(next);
  };
  const restoreFocus = () => {
    const row = Array.from(pane.current?.querySelectorAll<HTMLElement>("[data-view-key]") ?? []).find((element) => element.dataset.viewKey === openedKey.current);
    const target = opener.current?.isConnected && !opener.current.closest("[hidden]") ? opener.current : row;
    target?.focus({ preventScroll: true });
    opener.current = null;
  };
  const closeInfo = () => {
    onInfoClose();
    if (selected?.displayLayer === null) closeDetail();
  };
  const status = inspection?.configuration_status ?? "unknown";
  const notices = <>
    {error && <Alert type="error" content={"配置读取失败：" + error.message} action={<Button size="small" onClick={onRefresh}>重试</Button>} />}
    {!!inspection?.errors.length && <Alert type="warning" content={inspection.errors.map((item) => item.message).join("；")} />}
    {inspection?.sanitization_truncated && <Alert type="warning" content="配置响应已截断，当前仅显示已取得的字段。" />}
    {!!inspection?.current?.validation?.length && <Alert type="warning" content={inspection.current.validation.map((item) => item.field + "：" + item.message).join("；")} />}
  </>;

  return <>
    <section ref={pane} hidden={!visible} className="obs-configuration" aria-label="分层配置">
      <div className="obs-config-toolbar">
        <div className="obs-pane-heading"><strong>当前配置</strong>
          <Tag color={status === "applied" ? "green" : status === "pending" ? "orange" : "gray"}>{status === "applied" ? "已应用" : status === "pending" ? "待应用" : "暂无法确认"}</Tag>
          <Button size="small" onClick={onRefresh}>刷新</Button>
        </div>
        <div className="obs-config-version"><span>版本 {inspection?.current?.configuration_revision?.slice(0, 10) ?? "未记录"}</span>
          {inspection && <span>更新于 {dateTime(inspection.generated_at)}</span>}</div>
      </div>
      {status === "pending" && <Alert type="warning" content="最新保存的配置尚未应用到服务。" />}
      {inspection && status === "unknown" && <Alert type="info" content={inspection.configuration_status_reason || "运行状态暂未更新，配置按当前保存的设置展示。"} />}
      {notices}
      {loading ? <Spin /> : !inspection?.current ? <Empty description="未取得当前配置" /> : <>
        {missing && <Alert type="info" content={"未记录此组件的配置：" + params.get("entity")} />}
        <Input aria-label="搜索四层配置" placeholder="搜索全部配置：名称、字段、模型或组件" allowClear value={search} onChange={setSearch} />
        {!!search.trim() && <section className="config-search-results" aria-label="配置搜索结果">
          <div className="obs-pane-heading"><strong>搜索结果</strong><span className="obs-muted">{matches.length} 项</span></div>
          {matches.length ? matches.map((entity) => <button type="button" key={entity.viewKey} onClick={(event) => openEntity(entity, event.currentTarget)}>
            <strong>{entityName(entity)}</strong><span>{entityLocation(entity)}</span>
          </button>) : <Empty description="没有匹配的配置" />}
        </section>}
        <div className="config-workspace">
          <nav className="config-layer-nav" aria-label="四层配置导航">
            {CONFIGURATION_LAYERS.map((item) => <button key={item.id} type="button" aria-current={layer === item.id ? "page" : undefined}
              onClick={() => chooseLayer(item.id)}><strong>{item.name}</strong><span>{item.description}</span></button>)}
          </nav>
          <div className="config-layer-select"><Select aria-label="选择配置层" value={layer} onChange={chooseLayer}
            options={CONFIGURATION_LAYERS.map((item) => ({ value: item.id, label: item.name }))} /></div>
          <div className="config-layer-content">
            <header><h3>{layerDefinition.name}</h3><p className="obs-muted">{layerDefinition.description}</p></header>
            {layer === "agent" && <p className="config-value-heading">保存配置解析值 · 模型及参数只读。实例默认值不代表每个会话的实际选择。</p>}
            {layerDefinition.groups.map(([id, name]) => {
              const rows = entities.filter((entity) => entity.group === id);
              return <section key={id} className="config-group" aria-label={name}>
                <div className="obs-pane-heading"><h4>{name}</h4>{groupActions(id)}</div>
                {rows.length ? rows.map((entity) => <article key={entity.viewKey} className={"config-row" + (selected?.viewKey === entity.viewKey ? " is-selected" : "")}>
                  <div className="config-row-main"><button type="button" className="obs-link" data-view-key={entity.viewKey}
                    onClick={(event) => openEntity(entity, event.currentTarget)}>{entityName(entity)}</button>
                    <span className="config-row-summary" title={configurationSummary(entity)}>{configurationSummary(entity)}</span></div>
                  <EntityState entity={entity} />
                  <Button size="small" aria-label={`查看 ${entityName(entity)}`} onClick={(event) => openEntity(entity, event.currentTarget instanceof HTMLElement ? event.currentTarget : undefined)}>查看详情</Button>
                  {layer === "agent" && <div className="config-row-fields">
                    {entity.usage && <p className="config-usage">{entity.usage}</p>}
                    {entity.applicability && <p className="config-applicability">{entity.applicability}</p>}
                    <ConfigurationFields entity={entity} inline />
                  </div>}
                </article>) : <p className="config-group-empty">未配置</p>}
                {groupFooter(id)}
              </section>;
            })}
          </div>
        </div>
      </>}
    </section>
    <Drawer title={selected ? entityName(selected) : "配置详情"} visible={visible && !!selected?.displayLayer} width={narrow ? "100vw" : 520}
      className="config-detail-drawer" footer={null} onCancel={closeDetail} afterClose={restoreFocus} escToExit autoFocus unmountOnExit>
      {selected && <>
        <p className="obs-muted">{entityLocation(selected)}</p>
        <EntityState entity={selected} />
        <div className="config-detail-actions">{entityActions(selected, navigate)}</div>
        {selected.usage && <p className="config-usage">{selected.usage}</p>}
        {selected.applicability && <p className="config-applicability">{selected.applicability}</p>}
        {selected.runtime_stale && <p className="obs-muted">运行快照过期，服务当前值暂无法确认。</p>}
        <ConfigurationFields entity={selected} />
        {selected.runtime && !selected.field_sources && <><h4>运行信息</h4><ConfigurationValues value={selected.runtime} /></>}
      </>}
    </Drawer>
    <Drawer title="实例信息" visible={infoOpen} width={narrow ? "100vw" : 520} className="config-detail-drawer"
      footer={null} onCancel={closeInfo} afterClose={() => { if (infoOpener.current?.isConnected) infoOpener.current.focus({ preventScroll: true }); else restoreFocus(); }} escToExit unmountOnExit>
      {notices}
      {loading ? <Spin /> : !instanceEntities.length ? <Empty description="未取得实例信息" /> : instanceEntities.map((entity) =>
        <section key={entity.viewKey} className="config-instance-section"><h4>{entityName(entity)}</h4><ConfigurationFields entity={entity} /></section>)}
    </Drawer>
  </>;
}
