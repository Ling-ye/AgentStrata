import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Button, Message, Tag } from "@arco-design/web-react";
import ServiceCard from "../components/ServiceCard";
import { api, streamInfraLogs, streamTask } from "../api";
import type { InfraService, Task } from "../types";
import { useEventStreamLines } from "../shared/hooks/useEventStreamLines";
import LogDrawer from "../shared/ui/LogDrawer";
import PageSection from "../shared/ui/PageSection";
import TaskStreamSheet from "../shared/ui/TaskStreamSheet";

interface Props {
  visible?: boolean;
}

export default function ServicesPage({ visible = true }: Props) {
  const [services, setServices] = useState<InfraService[]>([]);
  const [busy, setBusy] = useState<Record<string, boolean>>({});
  const [composeUpBusy, setComposeUpBusy] = useState(false);
  const [lastRefreshed, setLastRefreshed] = useState<Date | null>(null);
  const logStream = useEventStreamLines();
  const taskStream = useEventStreamLines();

  const servicesQuery = useQuery({
    queryKey: ["infra-services"],
    queryFn: api.infraServices,
    enabled: visible,
    refetchInterval: visible ? 15_000 : false,
  });
  const servicesData = servicesQuery.data ?? services;

  useEffect(() => {
    if (!servicesQuery.data) return;
    setServices(servicesQuery.data);
    setLastRefreshed(new Date());
  }, [servicesQuery.data]);

  const setBusyFor = (id: string, value: boolean) =>
    setBusy((prev) => ({ ...prev, [id]: value }));

  const openTask = (service: InfraService, kind: string, task: Task) => {
    taskStream.start(
      (onLine, _onStatus, onEnd) =>
        streamTask(
          task.id,
          onLine,
          () => {
            onEnd();
            void servicesQuery.refetch();
          },
        ),
      { title: `${service.display_name} · ${kind}`, running: true },
    );
  };

  const handleAction = async (service: InfraService, verb: string) => {
    if (verb === "doctor") return;
    setBusyFor(service.id, true);
    try {
      if (verb === "pull") {
        const result = await api.infraAction(service.id, verb);
        if ("id" in result) {
          openTask(service, verb, result as Task);
        }
      } else {
        await api.infraAction(service.id, verb);
        Message.success(`${service.display_name}：${verb} 成功`);
        await servicesQuery.refetch();
      }
    } catch (e) {
      Message.error(`${service.display_name}：${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusyFor(service.id, false);
    }
  };

  const openLogs = (service: InfraService) => {
    logStream.start(
      (onLine, onStatus) => streamInfraLogs(service.id, onLine, onStatus),
      { title: `${service.display_name} · 日志` },
    );
  };

  const handleComposeUpAll = async () => {
    setComposeUpBusy(true);
    try {
      await api.infraComposeUp();
      Message.success("Docker Compose 全部服务已启动");
      await servicesQuery.refetch();
    } catch (e) {
      Message.error(`启动失败：${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setComposeUpBusy(false);
    }
  };

  const healthyCount = useMemo(
    () => servicesData.filter((service) => service.color === "green").length,
    [servicesData],
  );
  const problemCount = useMemo(
    () => servicesData.filter((service) => service.color === "red" || service.color === "yellow").length,
    [servicesData],
  );
  return (
    <>
      <PageSection
        title="基础设施服务"
        description="共享 Docker MCP 与平台网关，每张卡片可独立诊断和管理登录。"
        extra={
          <>
            <Button
              size="small"
              loading={composeUpBusy}
              onClick={() => void handleComposeUpAll()}
            >
              启动全部 Docker 服务
            </Button>
            <Tag className="cc-status-tag" color="green">正常 {healthyCount}</Tag>
            <Tag className="cc-status-tag" color={problemCount > 0 ? "red" : "gray"}>需关注 {problemCount}</Tag>
            <Tag className="cc-tag-meta">总计 {servicesData.length}</Tag>
            {lastRefreshed && (
              <span className="infra-refresh-text">
                更新于 {lastRefreshed.toLocaleTimeString()}
              </span>
            )}
          </>
        }
      >
        <div className="infra-grid">
          {servicesData.map((service) => (
            <div key={service.id} id={`infra-${service.id}`}>
              <ServiceCard
                service={service}
                busy={!!busy[service.id]}
                onAction={(verb) => void handleAction(service, verb)}
                onLogs={() => openLogs(service)}
              />
            </div>
          ))}
        </div>
      </PageSection>

      <LogDrawer
        title={logStream.title}
        visible={logStream.open}
        status={logStream.status}
        lines={logStream.lines}
        onClose={logStream.close}
      />

      <TaskStreamSheet
        title={taskStream.title}
        visible={taskStream.open}
        running={taskStream.running}
        lines={taskStream.lines}
        onClose={taskStream.close}
      />
    </>
  );
}
