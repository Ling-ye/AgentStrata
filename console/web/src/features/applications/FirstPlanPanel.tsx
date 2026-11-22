import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Card, Input, Select, Space, Spin, Tag, Typography } from "@arco-design/web-react";
import { applicationsApi, type FirstPlan, type PlanDelivery, type InstallPlatform } from "./api";
import { managementStateLabel } from "./navigation";

const targets = [
  { value: "web", label: "网页应用" },
  { value: "desktop", label: "Windows 桌面应用" },
  { value: "source", label: "独立服务应用" },
  { value: "qq_official", label: "QQ 官方机器人" },
];
const platformNames: Record<InstallPlatform, string> = { "windows-x64": "Windows x64", "linux-x64": "Linux x64" };
const deliveryFields = [["entry", "启动入口"], ["services", "所需服务和组件"], ["bindings", "接收方首次配置"]] as const;
type Editable = Pick<FirstPlan, "revision" | "name" | "target" | "scope" | "acceptance"> & { delivery: PlanDelivery };
export default function FirstPlanPanel({ id, onDirty }: { id: string; onDirty: (dirty: boolean) => void }) {
  const client = useQueryClient(),
    gate = useRef(false);
  const query = useQuery({
    queryKey: ["first-plan", id],
    queryFn: ({ signal }) => applicationsApi.firstPlan(id, signal),
    retry: false,
    refetchInterval: q =>
      ["planning", "initializing", "starting", "developing", "packaging", "verifying"].includes(
        q.state.data?.state || "",
      )
        ? 2000
        : false,
  });
  const [edit, setEdit] = useState<Editable>(),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false);
  useEffect(() => {
    onDirty(!!edit || busy);
    return () => onDirty(false);
  }, [edit, busy, onDirty]);
  const p = query.data;
  async function act(action: "edit" | "confirm" | "retry") {
    if (!p || gate.current) return;
    gate.current = true;
    setBusy(true);
    setError("");
    try {
      const updated = await applicationsApi.reviewPlan(
        id,
        action,
        action === "edit" && edit ? edit : { revision: p.revision },
      );
      client.setQueryData(["first-plan", id], updated);
      setEdit(undefined);
      await Promise.all([
        client.invalidateQueries({ queryKey: ["application", id] }),
        client.invalidateQueries({ queryKey: ["applications"] }),
      ]);
    } catch (e) {
      setError(String(e));
      void query.refetch();
    } finally {
      gate.current = false;
      setBusy(false);
    }
  }
  if (!p) return query.isError ? <Alert type="error" content={String(query.error)} /> : <Spin />;
  return (
    <Card title="首版方案" extra={<Tag>{managementStateLabel(p.state)}</Tag>} className="application-form">
      <Space direction="vertical" size={14} style={{ width: "100%" }}>
        <Typography.Text bold>原始需求</Typography.Text>
        <p className="application-prose">{p.requirement}</p>
        {edit ? (
          <>
            <label>
              应用名称
              <Input value={edit.name} onChange={name => setEdit({ ...edit, name })} />
            </label>
            <label>
              应用形态
              <Select
                value={edit.target}
                options={targets}
                onChange={target => setEdit({ ...edit, target, delivery: { ...edit.delivery, platforms: target === "desktop" ? ["windows-x64"] : edit.delivery.platforms } })}
              />
            </label>
            <label>
              首版范围
              <Input.TextArea value={edit.scope} onChange={scope => setEdit({ ...edit, scope })} />
            </label>
            <label>
              怎样验收
              <Input.TextArea
                value={edit.acceptance}
                onChange={acceptance => setEdit({ ...edit, acceptance })}
              />
            </label>
            <label>交付平台
              <Select mode="multiple" aria-label="交付平台" value={edit.delivery.platforms}
                options={Object.entries(platformNames).filter(([key]) => edit.target !== "desktop" || key === "windows-x64")
                  .map(([value, label]) => ({ value, label }))}
                onChange={(platforms: InstallPlatform[]) => setEdit({ ...edit, delivery: { ...edit.delivery, platforms } })} />
            </label>
            {deliveryFields.map(([key, label]) => <label key={key}>{label}
              <Input.TextArea aria-label={label} value={edit.delivery[key]} onChange={value => setEdit({ ...edit, delivery: { ...edit.delivery, [key]: value } })} />
            </label>)}
            <Space>
              <Button
                type="primary"
                loading={busy}
                disabled={!edit.name.trim() || !edit.scope.trim() || !edit.acceptance.trim() || !edit.delivery.platforms.length || deliveryFields.some(([key]) => !edit.delivery[key].trim())}
                onClick={() => void act("edit")}
              >
                保存方案
              </Button>
              <Button disabled={busy} onClick={() => setEdit(undefined)}>
                取消修改
              </Button>
            </Space>
          </>
        ) : (
          <>
            {p.scope && (
              <>
                <Typography.Text bold>
                  首版范围 · {targets.find(t => t.value === p.target)?.label}
                </Typography.Text>
                <p className="application-prose">{p.scope}</p>
                <Typography.Text bold>怎样验收</Typography.Text>
                <p className="application-prose">{p.acceptance}</p>
                {p.delivery && <dl className="studio-info">
                  <dt>交付平台</dt><dd>{p.delivery.platforms.map(key => platformNames[key]).join("、")}</dd>
                  {deliveryFields.map(([key, label]) => <div style={{ display: "contents" }} key={key}><dt>{label}</dt><dd>{p.delivery![key]}</dd></div>)}
                </dl>}
                {!p.delivery && p.state === "plan_review" && <Alert content="请先调整方案，补全交付信息。" />}

              </>
            )}
            {p.state === "planning" && <Spin tip="正在生成方案…" />}
            {["plan_failed", "initialization_failed", "failed"].includes(p.state) && (
              <Alert
                type="warning"
                content={p.reply || "处理失败，请核对连接后重试。"}
                action={<Button href="#models">模型与账号</Button>}
              />
            )}
            <Space wrap>
              {["plan_failed", "plan_review"].includes(p.state) && (
                <Button
                  onClick={() =>
                    setEdit({
                      revision: p.revision,
                      name: p.name,
                      target: p.target || "web",
                      scope: p.scope || p.requirement,
                      acceptance: p.acceptance,
                      delivery: p.delivery ?? { platforms: p.target === "desktop" ? ["windows-x64"] : ["windows-x64", "linux-x64"], entry: "", services: "", bindings: "" },
                    })
                  }
                >
                  调整方案
                </Button>
              )}
              {p.state === "plan_failed" && (
                <Button loading={busy} onClick={() => void act("retry")}>
                  重新生成
                </Button>
              )}
              {["plan_review", "initialization_failed"].includes(p.state) && (
                <Button type="primary" disabled={!p.delivery && !p.approved} loading={busy} onClick={() => void act("confirm")}>
                  {p.state === "initialization_failed"
                    ? "继续已确认的方案"
                    : p.target === "qq_official"
                      ? "确认并应用配置"
                      : "确认并开始开发"}
                </Button>
              )}
            </Space>
          </>
        )}
        {error && <Alert type="error" content={error} />}
      </Space>
    </Card>
  );
}
