import { useEffect, useRef, useState } from "react";
import { Alert, Button, Card, Space, Typography } from "@arco-design/web-react";
import { useQueryClient } from "@tanstack/react-query";
import { applicationHref } from "./navigation";
export default function NativeConnection() {
  const [busy, setBusy] = useState(false); const [error, setError] = useState(""); const gate = useRef(false);
  const alive = useRef(true);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  const client = useQueryClient();
  async function connect() {
    if (gate.current) return; gate.current = true; setBusy(true); setError("");
    try {
      const response = await fetch("/api/applications/connections/native", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
      const result = await response.json();
      if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : "连接失败，请确认桌宠后台正在运行。");
      if (result.canceled) return;
      void client.invalidateQueries({ queryKey: ["applications"] }); if (alive.current) window.location.hash = applicationHref(result.id, "records");
    } catch (e) { if (alive.current) setError(e instanceof Error ? e.message : "连接失败"); }
    finally { gate.current = false; if (alive.current) setBusy(false); }
  }
  return <Space direction="vertical" size={16} style={{ width: "100%" }}><Card title="已有 QQ 应用"><Typography.Paragraph>当前工作台管理的 QQ 应用会显示在“我的应用”中。</Typography.Paragraph><Button href="#applications">查看我的应用</Button></Card>
    <Card title="Windows 桌宠"><Typography.Paragraph>先启动支持工作台接入的桌宠客户端，再选择该应用的安装目录。接入后可以查看后台记录和调整模型、人设与回复风格。</Typography.Paragraph>
      <Button type="primary" loading={busy} onClick={() => void connect()}>选择并连接桌宠</Button>{error && <Alert type="error" content={error} />}</Card>
  </Space>;
}
