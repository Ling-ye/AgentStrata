import { useEffect, useRef, useState } from "react";
import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Empty, Input, Modal, Select, Space, Spin, Tabs } from "@arco-design/web-react";
import { IconApps, IconBook, IconList, IconMessage, IconRobot, IconTool } from "@arco-design/web-react/icon";
import { applicationsApi, openApplicationUrl, ApplicationRequestError, type ApplicationView, type DraftRequest } from "./api";
import { applicationHref, managementStateLabel, stateLabel, typeLabel } from "./navigation";
import NativeConnection from "./NativeConnection";
import Live2DImport from "./Live2DImport";

const icons = { box: IconApps, chat: IconMessage, book: IconBook, cat: IconRobot, tools: IconTool };
const iconNames: Record<string, string> = {
  box: "应用",
  chat: "对话",
  book: "书本",
  cat: "桌宠",
  tools: "工具",
};
export function AppIcon({ app }: { app: Pick<ApplicationView, "icon" | "type" | "id"> }) {
  const key =
    app.icon ||
    (app.type.includes("qq") || app.type === "weixin"
      ? "chat"
      : app.type === "companion" || app.id.includes("live2d")
        ? "cat"
        : app.id.includes("reader")
          ? "book"
          : "box");
  const Icon = icons[key as keyof typeof icons] ?? IconApps;
  return (
    <span className={"studio-app-icon icon-" + (app.type === "weixin" ? "weixin" : key)} aria-hidden="true">
      <Icon />
    </span>
  );
}
const pendingKey = "agentstrata.studio.application-draft";
const initial = () => ({ name: "", requirement: "", icon: "box" });

export default function ApplicationDirectory({
  archived = false,
  create = false,
  connect = false,
}: {
  archived?: boolean;
  create?: boolean;
  connect?: boolean;
}) {
  const client = useQueryClient();
  const list = useQuery({
    queryKey: ["applications", archived],
    queryFn: ({ signal }) => applicationsApi.list(signal, archived),
    refetchInterval: 10000,
  });
  const archive = useMutation({
    mutationFn: (change: { id: string; archived: boolean }) => applicationsApi.setArchived(change.id, change.archived),
    onSettled: () => client.invalidateQueries({ queryKey: ["applications"] }),
  });
  const [search, setSearch] = useState(""),
    [grid, setGrid] = useState(false),
    [filter, setFilter] = useState(""),
    [sort, setSort] = useState(false);
  const [draft, setDraft] = useState(initial),
    [pending, setPending] = useState<DraftRequest>(),
    [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const gate = useRef(false),
    alive = useRef(false);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  useEffect(() => {
    if (!create) return;
    try {
      const raw = sessionStorage.getItem(pendingKey);
      if (raw) {
        const saved = JSON.parse(raw) as DraftRequest;
        if (!/^[\da-f]{32}$/.test(saved.id) || typeof saved.requirement !== "string")
          throw new Error("原创建记录无法读取，请核对应用列表。");
        setPending(saved);
        setDraft(saved);
      }
    } catch (e) {
      setError(String(e));
    }
  }, [create]);
  const close = () => {
    if (!gate.current) window.location.hash = "applications";
  };
  async function submit() {
    if (gate.current || !draft.requirement.trim()) return;
    const body = pending ?? {
      ...draft,
      requirement: draft.requirement.trim(),
      id: crypto.randomUUID().replace(/-/g, ""),
    };
    gate.current = true;
    setBusy(true);
    setError("");
    try {
      sessionStorage.setItem(pendingKey, JSON.stringify(body));
      setPending(body);
      const app = await applicationsApi.draft(body);
      sessionStorage.removeItem(pendingKey);
      setPending(undefined);
      setDraft(initial());
      void client.invalidateQueries({ queryKey: ["applications"] });
      if (alive.current && window.location.hash.startsWith("#create"))
        window.location.hash = applicationHref(app.id, "improve");
    } catch (e) {
      if (e instanceof ApplicationRequestError && [400, 422].includes(e.status)) {
        sessionStorage.removeItem(pendingKey);
        setPending(undefined);
      }
      setError(String(e));
    } finally {
      gate.current = false;
      if (alive.current) setBusy(false);
    }
  }
  const catalog = list.data?.applications ?? [];
  const native = catalog.filter(app => app.source === "native");
  const connections = useQueries({
    queries: native.map(app => ({
      queryKey: ["application", app.id],
      queryFn: ({ signal }: { signal: AbortSignal }) => applicationsApi.get(app.id, signal),
      refetchInterval: 10000,
      retry: false,
    })),
  });
  const live = new Map(
    native.map((app, i) => [
      app.id,
      connections[i].data ??
        (connections[i].isError ? { ...app, state: { ...app.state, process: "unavailable" } } : app),
    ]),
  );
  const all = catalog.map(app => live.get(app.id) ?? app);
  const apps = all.filter(
    a =>
      (!filter || a.type === filter) &&
      (a.name + " " + a.id).toLowerCase().includes(search.trim().toLowerCase()),
  );
  if (sort) apps.sort((a, b) => a.name.localeCompare(b.name, "zh-CN"));
  const status = (a: ApplicationView) =>
    a.plan_state ? managementStateLabel(a.plan_state) : stateLabel(a.state.process);
  const href = (a: ApplicationView) =>
    applicationHref(
      a.id,
      a.source === "release" ? "use" : a.first_plan && !["ready", "applied"].includes(a.plan_state || "") ? "improve" : "overview",
    );
  const archiveLabel = archived ? "移回我的应用" : "归档";
  const actions = (app: ApplicationView) => (
    <span className="studio-app-actions" onClick={event => event.stopPropagation()}>
      {app.source === "release" && <>
        <a href={openApplicationUrl(app.id)} target="_blank" rel="noopener noreferrer">打开应用</a>
        <a href={applicationHref(app.id, "use")}>下载与部署</a>
      </>}
      <Button
        type="text"
        size="mini"
        aria-label={archiveLabel + " " + app.name}
        loading={archive.isPending && archive.variables?.id === app.id}
        disabled={archive.isPending}
        onClick={() => archive.mutate({ id: app.id, archived: !archived })}
      >
        {archiveLabel}
      </Button>
    </span>
  );
  return (
    <main className="studio-directory">
      <h1>{archived ? "已归档应用" : "我的应用"}</h1>
      <section className="studio-paper">
        <div className="studio-toolbar">
          <Space wrap>
            {!archived && <>
              <Button type="primary" href="#create">＋ 创建应用</Button>
              <Button href="#connect">接入已有应用</Button>
            </>}
            <Button href={archived ? "#applications" : "#applications?archived=1"}>
              {archived ? "返回我的应用" : "已归档"}
            </Button>
          </Space>
          <Space wrap>
            <Input.Search
              style={{ width: "min(240px, 100%)" }}
              aria-label="搜索应用名称或 ID"
              placeholder="搜索应用名称或 ID"
              value={search}
              onChange={setSearch}
              allowClear
            />
            <Select
              aria-label="筛选应用形态"
              value={filter}
              onChange={setFilter}
              options={[
                { value: "", label: "全部形态" },
                ...Array.from(new Set(all.map(a => a.type)), t => ({ value: t, label: typeLabel(t) })),
              ]}
            />
            <Button aria-pressed={sort} onClick={() => setSort(!sort)}>
              名称排序
            </Button>
            <Button aria-label={grid ? "列表视图" : "卡片视图"} onClick={() => setGrid(!grid)}>
              {grid ? <IconList /> : <IconApps />}
            </Button>
          </Space>
        </div>
        {archive.isError && <Alert type="error" content={String(archive.error)} />}
        {list.isPending ? (
          <Spin />
        ) : list.error ? (
          <Alert
            type="error"
            content={String(list.error)}
            action={<Button onClick={() => void list.refetch()}>重试</Button>}
          />
        ) : !apps.length ? (
          <Empty description={all.length ? "没有匹配的应用" : archived ? "没有已归档的应用" : "还没有应用，点击创建应用开始。"} />
        ) : grid ? (
          <div className="studio-app-grid">
            {apps.map(app => (
              <div className="studio-app-card" key={app.id}>
                <a href={href(app)}><AppIcon app={app} /><strong>{app.name}</strong><small>{status(app)}</small></a>
                {actions(app)}
              </div>
            ))}
          </div>
        ) : (
          <table className="studio-app-table">
            <thead>
              <tr>
                <th>图标</th>
                <th>名称</th>
                <th>应用形态</th>
                <th>状态</th>
                <th>最近活动</th>
              </tr>
            </thead>
            <tbody>
              {apps.map(a => (
                <tr
                  key={a.id}
                  onClick={() => {
                    window.location.hash = href(a);
                  }}
                >
                  <td>
                    <AppIcon app={a} />
                  </td>
                  <td>
                    <a href={href(a)}>{a.name}</a>
                    {actions(a)}
                  </td>
                  <td>{typeLabel(a.type)}</td>
                  <td>{status(a)}</td>
                  <td>{a.last_activity ? new Date(a.last_activity * 1000).toLocaleString() : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
      <Modal
        title="创建应用"
        visible={create}
        onCancel={close}
        onOk={() => void submit()}
        okText={pending ? "继续原创建请求" : "生成首版方案"}
        cancelText="取消"
        okButtonProps={{ loading: busy, disabled: !draft.requirement.trim() }}
        maskClosable={false}
        className="studio-dialog"
      >
        <label>
          应用名称（选填）
          <Input
            value={draft.name}
            disabled={!!pending}
            onChange={name => setDraft({ ...draft, name })}
            placeholder="留空时根据需求命名"
          />
        </label>
        <label>
          你想做一个什么应用？
          <Input.TextArea
            value={draft.requirement}
            disabled={!!pending}
            onChange={requirement => setDraft({ ...draft, requirement })}
            autoSize={{ minRows: 4, maxRows: 10 }}
            placeholder="例如：做一个网页报修台，同事可以提交故障，维修人员可以标记完成。"
          />
        </label>
        <label>应用图标</label>
        <Space>
          {Object.entries(icons).map(([key, Icon]) => (
            <Button
              key={key}
              aria-label={iconNames[key]}
              aria-pressed={draft.icon === key}
              disabled={!!pending}
              type={draft.icon === key ? "primary" : "secondary"}
              onClick={() => setDraft({ ...draft, icon: key })}
            >
              <Icon />
            </Button>
          ))}
        </Space>
        {error && <Alert type="error" content={error} />}
      </Modal>
      <Modal title="接入已有应用" visible={connect} onCancel={close} footer={null} className="studio-dialog">
        {connect && (
          <Tabs>
            <Tabs.TabPane key="connect" title="连接已安装应用">
              <NativeConnection />
            </Tabs.TabPane>
            <Tabs.TabPane key="import" title="导入桌宠开发源码">
              <Live2DImport />
            </Tabs.TabPane>
          </Tabs>
        )}
      </Modal>
    </main>
  );
}
