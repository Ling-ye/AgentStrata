import { useMemo, useState } from "react";
import { Button, Drawer, Empty, Input, Space, Spin, Tabs, Tag, Typography } from "@arco-design/web-react";
import { useQueries, useQuery } from "@tanstack/react-query";
import { configurationTabForEntity } from "../features/architecture/configurationPresentation";
import { api } from "../api";
import { catalogLayer, layerName } from "../features/architecture/model";
import { IconSearch } from "@arco-design/web-react/icon";
import CatalogCard from "../components/CatalogCard";
import { catalogSurface, type CatalogSurface } from "../features/catalog/surfaces";
import { useCatalog } from "../features/catalog/useCatalog";
import PageSection from "../shared/ui/PageSection";
import type { CatalogItem } from "../types";

const { Text } = Typography;

const ALL_TAB = "全部";
const ALL_CATEGORY = "全部分类";

const SURFACE_TABS: Array<{ key: CatalogSurface | typeof ALL_TAB; label: string }> = [
  { key: ALL_TAB, label: ALL_TAB },
  { key: "tools", label: "工具" },
  { key: "prompts", label: "提示词" },
  { key: "agents", label: "Agent" },
  { key: "context", label: "上下文" },
];

function extractCategories(items: CatalogItem[]): string[] {
  const seen = new Set<string>();
  for (const item of items) {
    if (item.category) seen.add(item.category);
  }
  return Array.from(seen).sort();
}

interface Props {
  visible?: boolean;
}

export default function ToolsPage({ visible }: Props) {
  const { data: catalog, isLoading, error } = useCatalog();
  const [search, setSearch] = useState("");
  const [surfaceTab, setSurfaceTab] = useState<string>(ALL_TAB);
  const [categoryTab, setCategoryTab] = useState(ALL_CATEGORY);
  const [layer, setLayer] = useState("");
  const [selectedItem, setSelectedItem] = useState<CatalogItem | null>(null);
  const bots = useQuery({ queryKey: ["bots"], queryFn: api.listBots, enabled: visible });

  const inspections = useQueries({ queries: (bots.data ?? []).map((bot) => ({ queryKey: ["inspection", bot.instance_id, ""], queryFn: () => api.inspection(bot.instance_id), enabled: visible, staleTime: 30_000 })) });
  const entityId = (item: CatalogItem) => item.kind === "prompt" ? "prompts:instance" : item.id.replace(/^tool_pack:/, "pack:").replace(/^tool_feature:/, "feature:").replace(/^sub:/, "subagent:");
  const bindings = (item: CatalogItem) => inspections.flatMap((query, index) => {
    const entity = query.data?.current?.entities.find((entry) => entry.id === entityId(item) || entry.config?.catalog_ref === item.id.slice(item.id.indexOf(":") + 1));
    const configured = entity?.configured && (item.kind !== "prompt" || !!entity.config?.[item.id.slice(8)]);
    return configured && bots.data?.[index] ? [{ bot: bots.data[index], index, entityId: entity!.id }] : [];
  });
  const surfaceItems = useMemo(() => {
    if (!catalog) return [];
    if (surfaceTab === ALL_TAB) return catalog;
    return catalog.filter((i) => catalogSurface(i) === surfaceTab);
  }, [catalog, surfaceTab]);

  const categories = useMemo(() => extractCategories(surfaceItems), [surfaceItems]);

  const filtered = useMemo(() => {
    let items = surfaceItems;
    if (layer) items = items.filter((item) => catalogLayer(item) === layer);
    if (categoryTab !== ALL_CATEGORY) {
      items = items.filter((i) => i.category === categoryTab);
    }
    if (search.trim()) {
      const q = search.trim().toLowerCase();
      items = items.filter(
        (i) =>
          i.name.toLowerCase().includes(q) ||
          i.description.toLowerCase().includes(q) ||
          i.category.toLowerCase().includes(q) ||
          catalogSurface(i).toLowerCase().includes(q) ||
          i.tools?.some((t) => t.name.toLowerCase().includes(q)),
      );
    }
    return items;
  }, [surfaceItems, categoryTab, search, layer]);

  const handleSurfaceChange = (next: string) => {
    setSurfaceTab(next);
    setCategoryTab(ALL_CATEGORY);
  };

  if (!visible) return null;

  return (
    <PageSection title="组件目录" description="工具、插件与实例配置">
      <Space direction="vertical" style={{ width: "100%" }} size={12}>
        <div className="obs-catalog-summary"><span>{catalog?.length ?? "—"} 个组件</span><Tag>{bots.data?.length ?? "—"} 个实例</Tag></div>
        <div className="run-layer-filter" aria-label="按后端层筛选组件">{["", "application", "agent", "capability"].map((id) => <button type="button" key={id} aria-pressed={layer === id} className={layer === id ? "is-selected" : ""} onClick={() => { setLayer(id); setCategoryTab(ALL_CATEGORY); }}>{id ? layerName(id) : "全部层"}<small>{catalog?.filter((item) => !id || catalogLayer(item) === id).length ?? 0}</small></button>)}</div>
        <Input
          prefix={<IconSearch />}
          placeholder="搜索组件名、描述、分类..."
          allowClear
          value={search}
          onChange={setSearch}
          style={{ maxWidth: 400 }}
        />

        <Tabs
          type="capsule"
          size="small"
          activeTab={surfaceTab}
          onChange={handleSurfaceChange}
        >
          {SURFACE_TABS.map((item) => (
            <Tabs.TabPane title={item.label} key={item.key} />
          ))}
        </Tabs>

        <Tabs
          type="line"
          size="small"
          activeTab={categoryTab}
          onChange={setCategoryTab}
        >
          <Tabs.TabPane title={ALL_CATEGORY} key={ALL_CATEGORY} />
          {categories.map((cat) => (
            <Tabs.TabPane title={cat} key={cat} />
          ))}
        </Tabs>

        {isLoading && <div className="panel-spinner"><Spin /></div>}
        {error && <Text type="error">加载失败: {String(error)}</Text>}

        {!isLoading && filtered.length === 0 && (
          <Empty description="没有匹配的组件" />
        )}

        <div className="obs-table-scroll"><table className="obs-table"><thead><tr><th>组件</th><th>类型 / 分组</th><th>配置实例</th><th>加载状态</th><th>操作</th></tr></thead>
          <tbody>{filtered.map((item) => {
            const assigned = bindings(item);
            const loaded = assigned.filter(({ index }) => !inspections[index]?.data?.loaded_stale && inspections[index]?.data?.loaded?.entities.some((entity) =>
              (entity.id === entityId(item) || entity.config?.catalog_ref === item.id.slice(item.id.indexOf(":") + 1)) && entity.loaded));
            return <tr key={item.id}><td><button className="obs-link" onClick={() => setSelectedItem(item)}>{item.name}</button><small>{item.id}</small></td>
              <td>{layerName(catalogLayer(item))}<small>{item.category}</small></td>
              <td>{bots.isLoading || inspections.some((query) => query.isLoading) ? "读取中" : bots.isError || inspections.some((query) => query.isError) ? "读取失败" : assigned.length ?
                <Space wrap>{assigned.map(({ bot, entityId: identity }) => <a key={bot.instance_id} href={`#bots?instance=${encodeURIComponent(bot.instance_id)}&tab=${configurationTabForEntity(identity)}&entity=${encodeURIComponent(identity)}`}>{bot.display_name}</a>)}</Space> : "未配置"}</td>
              <td>{loaded.length ? <Tag color="green">{loaded.length} 个实例已加载</Tag> : assigned.length ? <Tag>未确认</Tag> : "—"}</td>
              <td><Button size="small" onClick={() => setSelectedItem(item)}>详情</Button></td></tr>;
          })}</tbody></table></div>
        <Drawer title={selectedItem?.name ?? "组件详情"} visible={!!selectedItem} width="min(760px, 96vw)" footer={null} onCancel={() => setSelectedItem(null)}>
          {selectedItem && <CatalogCard item={selectedItem} />}
        </Drawer>
      </Space>
    </PageSection>
  );
}
