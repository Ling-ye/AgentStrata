import { useEffect, useRef, useState } from "react";
import { Button, Space, Spin, Tag } from "@arco-design/web-react";
import LogConsole from "./LogConsole";
import { api, streamTask } from "../api";
import type { InfraService, Task } from "../types";

interface Props {
  service: InfraService;
}

export default function DoctorPanel({ service }: Props) {
  const [open, setOpen] = useState(false);
  const [lines, setLines] = useState<string[]>([]);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const closer = useRef<(() => void) | null>(null);

  useEffect(() => {
    return () => { closer.current?.(); };
  }, []);

  const runDoctor = async () => {
    closer.current?.();
    setLines([]);
    setError(null);
    setRunning(true);
    setOpen(true);
    try {
      const result = await api.infraAction(service.id, "doctor");
      if ("id" in result) {
        const task = result as Task;
        closer.current = streamTask(
          task.id,
          (line) => setLines((prev) => [...prev, line]),
          () => setRunning(false),
        );
      } else {
        setLines(["诊断完成（无流式输出）"]);
        setRunning(false);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setRunning(false);
    }
  };

  if (!service.has_doctor) return null;

  return (
    <div className="doctor-panel">
      <Space>
        <Button
          size="small"
          type={service.state === "unhealthy" ? "primary" : "secondary"}
          status={service.state === "unhealthy" ? "warning" : undefined}
          loading={running}
          onClick={runDoctor}
        >
          诊断
        </Button>
        {lines.length > 0 && !open && (
          <Button size="small" type="text" onClick={() => setOpen(true)}>
            查看结果
          </Button>
        )}
      </Space>
      {open && (
        <div className="doctor-panel-content">
          <div className="doctor-panel-header">
            <Space>
              {running ? (
                <Tag color="blue"><Spin size={14} /> 运行中</Tag>
              ) : lines.length > 0 ? (
                <Tag color="green">已结束</Tag>
              ) : null}
            </Space>
            <Button size="small" type="text" onClick={() => setOpen(false)}>
              收起
            </Button>
          </div>
          {error && <div className="doctor-panel-error">{error}</div>}
          <LogConsole lines={lines} />
        </div>
      )}
    </div>
  );
}
