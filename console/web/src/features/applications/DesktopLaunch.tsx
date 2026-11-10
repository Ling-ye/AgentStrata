import { useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Alert, Button, Empty, Select, Space, Spin } from "@arco-design/web-react";
import { applicationsApi } from "./api";

export default function DesktopLaunch({ id }: { id: string }) {
  const builds = useQuery({ queryKey: ["application-management", id], queryFn: ({ signal }) => applicationsApi.messages(id, signal) });
  const [selected, setSelected] = useState("");
  const [busy, setBusy] = useState(false);
  const gate = useRef(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const ready = builds.data?.filter(row => row.state === "ready" && row.build === row.id) || [];
  async function operate(action: "bind") {
    if (gate.current || !selected) return;
    if (!window.confirm("选择这个构建已安装的 EXE。工作台将校验程序与源码文件，登记后不会自动启动；运行依赖尚无独立可信的校验依据。")) return;
    gate.current = true; setBusy(true); setNotice(""); setError("");
    try { const value = await applicationsApi.desktopAction(id, selected, action); setNotice(value.message); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { gate.current = false; setBusy(false); }
  }
  return <Space direction="vertical" style={{ width: "100%" }}>
    <Alert type="info" content="选择已安装的精确构建并绑定。仅支持带启动清单的新构建；旧构建仍从客户端打开。绑定仅核对程序与运行源码，不代表已准备依赖可信；不会自动安装或升级。" />
    {builds.isPending ? <Spin /> : builds.isError ? <Alert type="error" content="无法读取构建记录，请重试。" action={<Button onClick={() => void builds.refetch()}>重试</Button>} /> :
      !ready.length ? <Empty description="尚无可绑定的已就绪构建" /> :
      <><Select aria-label="桌面构建" value={selected || undefined} disabled={busy} placeholder="选择已安装的构建" onChange={value => { setSelected(value); setNotice(""); setError(""); }} options={ready.map(row => ({ value: row.id, label: row.id }))} />
        <Space><Button disabled={!selected || busy} onClick={() => void operate("bind")}>选择并校验已安装程序</Button><Button disabled title="准备的运行依赖缺少独立可信的校验依据">启动尚不可用</Button></Space></>}
    <Alert type="warning" content="启动尚不可用：准备的 Python、插件环境及 MCP 等运行依赖缺少独立可信的校验依据。请从已安装客户端打开；工作台不会用本地自写声明或首次绑定的文件摘要替代可信构建证据。" />
    {busy && <Spin />}{notice && <Alert type="info" content={notice} />}{error && <Alert type="error" content={error} />}
  </Space>;
}
