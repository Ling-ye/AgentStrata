import { useEffect, useState } from "react";
import { Button, Message, Modal, Spin, Tooltip, Typography } from "@arco-design/web-react";
import { api, streamConsoleLogs } from "../api";
import { useEventStreamLines } from "../shared/hooks/useEventStreamLines";
import LogDrawer from "../shared/ui/LogDrawer";
import PageSection from "../shared/ui/PageSection";

const { Text, Title } = Typography;

export default function SettingsPage() {
  const [consoleUpdating, setConsoleUpdating] = useState(false);
  const [reloadIn, setReloadIn] = useState(0);
  const logStream = useEventStreamLines();

  useEffect(() => {
    if (!consoleUpdating) return;
    if (reloadIn <= 0) {
      window.location.reload();
      return;
    }
    const timer = window.setTimeout(() => setReloadIn((n) => n - 1), 1000);
    return () => window.clearTimeout(timer);
  }, [consoleUpdating, reloadIn]);

  const handleConsoleUpdate = () => {
    Modal.confirm({
      title: "更新控制台",
      content:
        "将重建控制台前端并重启后端服务，期间本页面会短暂无法访问。完成后页面会自动刷新，是否继续？",
      okText: "更新并重启",
      cancelText: "取消",
      onOk: async () => {
        try {
          const res = await api.updateConsole();
          Message.info(res.message || "控制台更新中，稍后将自动刷新");
          setConsoleUpdating(true);
          setReloadIn(20);
        } catch (e) {
          Message.error(`触发更新失败：${e instanceof Error ? e.message : String(e)}`);
        }
      },
    });
  };

  const openConsoleLogs = () => {
    logStream.start(
      (onLine, onStatus) => streamConsoleLogs(onLine, onStatus),
      { title: "控制台日志" },
    );
  };

  return (
    <>
      <PageSection title="控制台设置">
        <div className="settings-grid">
          <div className="settings-item">
            <Title heading={6}>控制台日志</Title>
            <Text type="secondary">查看 chatcopilot-console.service 的实时日志输出。</Text>
            <div className="settings-action-row">
              <Button onClick={openConsoleLogs}>查看日志</Button>
            </div>
          </div>
          <div className="settings-item">
            <Title heading={6}>更新控制台</Title>
            <Text type="secondary">重建控制台前端并重启后端，期间本页面短暂不可用。</Text>
            <div className="settings-action-row">
              <Tooltip content="重建前端 + 重启后端服务">
                <Button onClick={handleConsoleUpdate} disabled={consoleUpdating}>
                  更新并重启
                </Button>
              </Tooltip>
            </div>
          </div>
        </div>
      </PageSection>

      <LogDrawer
        title={logStream.title}
        visible={logStream.open}
        status={logStream.status}
        lines={logStream.lines}
        onClose={logStream.close}
      />

      {consoleUpdating && (
        <div className="console-update-overlay">
          <Spin size={28} />
          <Text className="console-update-overlay-text">
            控制台更新中，{reloadIn}s 后自动刷新…
          </Text>
          <Button type="primary" onClick={() => window.location.reload()}>
            立即刷新
          </Button>
        </div>
      )}
    </>
  );
}
