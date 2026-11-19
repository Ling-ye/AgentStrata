import { applicationHref } from "./features/applications/navigation";
import { lazy, Suspense, useCallback, useEffect, useState } from "react";
import { Alert, Button, Spin } from "@arco-design/web-react";
import Sidebar, { type PageKey } from "./components/Sidebar";
import "./styles/studio.css";
import LegalInfo from "./components/LegalInfo";
import { NavigationGuardContext, useGuardedHash } from "./shared/navigationGuard";

const ApplicationsPage = lazy(() => import("./pages/ApplicationsPage"));
const OverviewPage = lazy(() => import("./pages/OverviewPage"));
const ServicesPage = lazy(() => import("./pages/ServicesPage"));
const BotsPage = lazy(() => import("./pages/BotsPage"));
const ToolsPage = lazy(() => import("./pages/ToolsPage"));
const SchedulesPage = lazy(() => import("./pages/SchedulesPage"));
const ModelsPage = lazy(() => import("./pages/ModelsPage"));
const SettingsPage = lazy(() => import("./pages/SettingsPage"));
const CodeHealthPage = lazy(() => import("./pages/CodeHealthPage"));


export default function App() {
  const { hash, navigate, registerGuard } = useGuardedHash();
  const candidate = hash.slice(1).split("?")[0];
  const params = new URLSearchParams(hash.split("?")[1]);
  const page: PageKey = [
    "applications",
    "create",
    "overview",
    "services",
    "bots",
    "tools",
    "evals",
    "harness",
    "code-health",
    "schedules",
    "models",
    "settings",
  ].includes(candidate)
    ? (candidate as PageKey)
    : "applications";
  const scopedApplication = new URLSearchParams(hash.split("?")[1]).get("application") ?? "";
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
    if (page === "bots") void checkBackend();
  }, [checkBackend, page]);

  const [linkError, setLinkError] = useState("");
  const legacyQuality = page === "evals" || page === "harness";
  useEffect(() => {
    if (!legacyQuality) return;
    const abort = new AbortController();
    const selection = new URLSearchParams(hash.split("?")[1]);
    const task = selection.get("task"), evaluation = selection.get("evaluation");
    setLinkError("");
    async function open() {
      let id = scopedApplication;
      if (!id && page === "harness" && task) {
        const { harnessApi } = await import("./features/harness/api");
        const record = await harnessApi.summary(task, abort.signal);
        if (record.source.kind === "code_health") {
          if (!abort.signal.aborted) window.location.replace("#code-health?" + new URLSearchParams({task}));
          return;
        }
        id = record.source.bot_id || "";
      } else if (!id && evaluation) {
        const { evaluationApi } = await import("./features/evals/evaluationApi");
        id = (await evaluationApi.get(evaluation, abort.signal)).bot_id;
      }
      if (abort.signal.aborted) return;
      const app = id.includes(":") ? id : "repo:" + id;
      window.location.replace(id ? applicationHref(app,page === "evals" ? "evaluations" : "repairs",
        { ...(task ? {task} : {}), ...(evaluation ? {evaluation} : {}) }) : "#applications");
    }
    void open().catch(e=>{if(!abort.signal.aborted)setLinkError(String(e));});
    return () => abort.abort();
  }, [hash, legacyQuality, page, scopedApplication]);
  const platform = ["models", "services", "tools", "code-health", "settings"].includes(page);
  const requestedSection = params.get("section");
  const modelSection = requestedSection === "profiles" || requestedSection === "account" ? requestedSection : "connections";
  const current =
    page === "models" && ["profiles", "account"].includes(modelSection)
      ? "#models?section=" + modelSection
      : "#" + page;
  const content = (
    <Suspense fallback={<Spin dot tip="正在加载页面…" />}>
      {(page === "applications" || page === "create") && (
        <ApplicationsPage hash={hash} create={page === "create"} connect={candidate === "connect"} />
      )}
      {page === "overview" && <OverviewPage onNavigate={navigate} visible />}
      {page === "services" && <ServicesPage visible scope="shared" />}
      {page === "bots" && <BotsPage loadError={loadError} visible />}
      {page === "tools" && <ToolsPage visible />}
      {legacyQuality && (linkError ? <Alert type="warning" content={linkError} action={<Button href="#applications">返回我的应用</Button>} /> : <Spin tip="正在定位应用…" />)}
      {page === "code-health" && (
        <>
          <div className="studio-scope-label">项目治理 / AgentStrata 全仓</div>
          <CodeHealthPage />
        </>
      )}
      {page === "schedules" && <SchedulesPage />}
      {page === "models" && <ModelsPage section={modelSection} />}
      {page === "settings" && <SettingsPage />}
    </Suspense>
  );
  return (
    <NavigationGuardContext.Provider value={registerGuard}>
      <div className="studio-frame">
        <header className="studio-topbar">
          <strong>
            AgentStrata <span>Studio</span>
          </strong>
          <nav aria-label="Studio 工作台">
            <a href="#applications" aria-current={!platform ? "page" : undefined}>
              我的应用
            </a>
            <a href="#models" aria-current={platform ? "page" : undefined}>
              平台管理
            </a>
          </nav>
          <LegalInfo />
        </header>
        {platform ? <Sidebar current={current}>{content}</Sidebar> : content}
      </div>
    </NavigationGuardContext.Provider>
  );
}
