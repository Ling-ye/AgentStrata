import { lazy, Suspense, useCallback, useEffect, useState } from "react";
import { Layout, Spin, Typography } from "@arco-design/web-react";
import Sidebar, { type PageKey } from "./components/Sidebar";

const OverviewPage = lazy(() => import("./pages/OverviewPage"));
const ServicesPage = lazy(() => import("./pages/ServicesPage"));
const BotsPage = lazy(() => import("./pages/BotsPage"));
const ToolsPage = lazy(() => import("./pages/ToolsPage"));
const SettingsPage = lazy(() => import("./pages/SettingsPage"));
const EvalsPage = lazy(() => import("./pages/EvalsPage"));

const { Sider, Content } = Layout;
const { Title, Text } = Typography;

export default function App() {
  const [page, setPage] = useState<PageKey>("overview");
  const [loadError, setLoadError] = useState<string | null>(null);

  const checkBackend = useCallback(async () => {
    try {
      await fetch("/api/bots");
      setLoadError(null);
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void checkBackend();
  }, [checkBackend]);

  return (
    <Layout className="console-layout">
      <Sider className="console-sider">
        <div className="console-logo">
          <Title heading={5} className="console-logo-title">AgentStrata</Title>
          <Text type="secondary" className="cc-text-small">运维控制台</Text>
        </div>
        <Sidebar current={page} onChange={setPage} />
      </Sider>
      <Content className="console-content">
        <Suspense fallback={<Spin dot tip="正在加载页面…" />}>
          {page === "overview" && <OverviewPage onNavigate={setPage} visible />}
          {page === "services" && <ServicesPage visible />}
          {page === "bots" && <BotsPage loadError={loadError} visible />}
          {page === "tools" && <ToolsPage visible />}
          {page === "evals" && <EvalsPage visible />}
          {page === "settings" && <SettingsPage />}
        </Suspense>
      </Content>
    </Layout>
  );
}
