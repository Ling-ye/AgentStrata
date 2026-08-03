import { useMemo, useState } from "react";
import { Empty, Input, Space, Spin, Tabs, Typography } from "@arco-design/web-react";
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

  const surfaceItems = useMemo(() => {
    if (!catalog) return [];
    if (surfaceTab === ALL_TAB) return catalog;
    return catalog.filter((i) => catalogSurface(i) === surfaceTab);
  }, [catalog, surfaceTab]);

  const categories = useMemo(() => extractCategories(surfaceItems), [surfaceItems]);

  const filtered = useMemo(() => {
    let items = surfaceItems;
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
  }, [surfaceItems, categoryTab, search]);

  const handleSurfaceChange = (next: string) => {
    setSurfaceTab(next);
    setCategoryTab(ALL_CATEGORY);
  };

  if (!visible) return null;

  return (
    <PageSection title="组件目录" description="按工具、提示词、Agent、上下文浏览 BotSpec 能力组件">
      <Space direction="vertical" style={{ width: "100%" }} size={12}>
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

        <div className="catalog-grid">
          {filtered.map((item) => (
            <CatalogCard key={item.id} item={item} />
          ))}
        </div>
      </Space>
    </PageSection>
  );
}
