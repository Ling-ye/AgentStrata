import { useEffect, useState } from "react";
import { Alert, Button, Card, Space, Spin } from "@arco-design/web-react";
export default function LauncherSession({ login, children }: { login?: () => Promise<void>; children: React.ReactNode }) {
  const [ready, setReady] = useState(!login);
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    if (!login) return;
    let current = true;
    setError("");
    login().then(() => { if (current) setReady(true); }, () => { if (current) setError("工作台登录未完成。请重试，或重新从本机启动器打开。"); });
    return () => { current = false; };
  }, [login, attempt]);
  if (ready) return <>{children}</>;
  return <Card title="AgentStrata Studio" style={{ maxWidth: 600, margin: "10vh auto" }}><Space direction="vertical">
    {error ? <><Alert type="error" content={error} /><Button onClick={() => setAttempt(value => value + 1)}>重试登录</Button></> : <Spin tip="正在验证本机启动入口…" />}
  </Space></Card>;
}
