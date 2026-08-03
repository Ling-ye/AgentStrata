import { Alert, Drawer, Space, Tag } from "@arco-design/web-react";
import LogConsole from "../../components/LogConsole";
import type { StreamStatus } from "../hooks/useEventStreamLines";

interface Props {
  title: string;
  visible: boolean;
  running: boolean;
  lines: string[];
  onClose: () => void;
  width?: number;
  status?: "running" | "done" | "failed";
  error?: string | null;
  streamStatus?: StreamStatus;
}

export default function TaskStreamSheet({
  title,
  visible,
  running,
  lines,
  onClose,
  width = 720,
  status,
  error,
  streamStatus,
}: Props) {
  const resolvedStatus = status ?? (running ? "running" : "done");
  const statusTag = resolvedStatus === "running"
    ? <Tag color="blue">运行中</Tag>
    : resolvedStatus === "failed"
      ? <Tag color="red">失败</Tag>
      : <Tag color="green">已结束</Tag>;

  return (
    <Drawer
      title={<Space wrap><span>{title}</span>{statusTag}</Space>}
      visible={visible}
      width={width}
      onCancel={onClose}
    >
      {streamStatus === "reconnecting" && (
        <Alert
          type="warning"
          content="日志连接中断，正在重连，后台任务未判失败。"
          showIcon
          style={{ marginBottom: 12 }}
        />
      )}
      {resolvedStatus === "failed" && error && (
        <Alert type="error" content={error} showIcon style={{ marginBottom: 12 }} />
      )}
      <LogConsole lines={lines} />
    </Drawer>
  );
}
