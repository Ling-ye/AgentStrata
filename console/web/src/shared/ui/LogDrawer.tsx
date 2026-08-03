import { Drawer, Space, Tag } from "@arco-design/web-react";
import LogConsole from "../../components/LogConsole";
import type { StreamStatus } from "../hooks/useEventStreamLines";

interface Props {
  title: string;
  visible: boolean;
  status: StreamStatus;
  lines: string[];
  onClose: () => void;
  width?: number;
}

function statusTag(status: StreamStatus) {
  if (status === "live") return <Tag color="green">实时</Tag>;
  if (status === "reconnecting") return <Tag color="orange">重连中</Tag>;
  return <Tag color="blue">连接中</Tag>;
}

export default function LogDrawer({ title, visible, status, lines, onClose, width = 720 }: Props) {
  return (
    <Drawer
      title={<Space wrap><span>{title}</span>{statusTag(status)}</Space>}
      visible={visible}
      width={width}
      onCancel={onClose}
    >
      <LogConsole lines={lines} />
    </Drawer>
  );
}
