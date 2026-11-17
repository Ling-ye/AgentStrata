import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Alert, Button, Card, Space, Spin, Tag, Typography } from "@arco-design/web-react";
import { credentialStateLabel, developmentSetupApi } from "./developmentSetup";
const { Text } = Typography;
export default function DevelopmentSetup({ configurationDirty }: { configurationDirty: boolean }) {
  const setup = useQuery({ queryKey: ["development-setup"], queryFn: ({ signal }) => developmentSetupApi.status(signal), refetchInterval: 10000, retry: false });
  const [busy, setBusy] = useState("");
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const gate = useRef(false);
  const alive = useRef(true);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  async function login(lane: "main" | "worker") {
    if (gate.current) return;
    gate.current = true; setBusy(lane); setNotice(""); setError("");
    try { const result = await developmentSetupApi.login(lane); if (alive.current) { setNotice(result.message); void setup.refetch(); } }
    catch (error) { if (alive.current) setError(error instanceof Error ? error.message : "授权窗口未打开，请重试。"); }
    finally { gate.current = false; if (alive.current) setBusy(""); }
  }
  return <Card title="桌面应用开发账号"><Space direction="vertical" size={16} style={{ width: "100%" }}>
    <Text>应用聊天、代码开发和独立测评分开配置。仅使用 API 的标准 QQ 应用不要求开发账号；桌面源码开发需要配置 Harness worker。这里不会复制另一套模型设置。</Text>
    {configurationDirty && <Alert type="warning" content="下方模型配置尚未保存。这里检查的是已保存配置，不代表草稿已生效。" />}
    {setup.isPending ? <Spin /> : setup.isError ? <Alert type="error" content="无法核对开发环境，请检查工作台连接。" /> : setup.data && <>
      <Alert type={setup.data.configured ? "info" : "warning"} content={setup.data.configured ? "聊天模型用途已配置；开发与测评请核对下列用途。凭据准备不代表真实模型执行已通过。" : "聊天模型用途尚未配置，请检查下方模型方案与用途引用。"} />
      {(["chat", "harness", "evaluation.judge"] as const).map(purpose => <Text key={purpose}>{purpose}：{setup.data.bindings[purpose] ? "已配置用途引用" : "尚未配置"}</Text>)}
      {(["main", "worker"] as const).map(lane => {
        const auth = setup.data.auth.find(item => item.lane === lane);
        return <Space wrap key={lane}><Text bold>{lane === "main" ? "主模型订阅账号" : "开发 Worker 订阅账号"}</Text>
          <Tag>{auth ? credentialStateLabel(auth.state) : "尚未检查"}</Tag>
          <Button disabled={!setup.data.login_available || !auth?.login_available || !!busy} loading={busy === lane} onClick={() => void login(lane)}>打开{lane === "main" ? "主模型" : "开发账号"}授权</Button>
        </Space>;
      })}
      {!setup.data.login_available && <Text type="secondary">当前环境不能打开 Windows 授权窗口，请使用已配置凭据或从本机启动器进入。</Text>}
    </>}
    <Button disabled={!!busy} onClick={() => void setup.refetch()}>重新检查授权状态</Button>
    {notice && <Alert type="info" content={notice} />}{error && <Alert type="error" content={error} />}
  </Space></Card>;
}
