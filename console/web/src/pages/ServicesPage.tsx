import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Alert, Button, Drawer, Empty, Message, Modal, Space, Spin, Tag } from "@arco-design/web-react";
import { serviceLayer } from "../features/architecture/model";
import { healthTagColor, infraStateLabel } from "../shared/ui/status";
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
  const [layer, setLayer] = useState("");
  const [selectedService, setSelectedService] = useState("");
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

  const executeAction = async (service: InfraService, verb: string) => {
    if (verb === "doctor") return;
    setBusyFor(service.id, true);
    try {
      if (verb === "pull") {
        const result = await api.infraAction(service.id, verb);
        if ("id" in result) {
          openTask(service, verb, result as Task);
        }
      } else if (verb === "recreate") {
        if (!service.instance_id) throw new Error("缺少 NapCat 实例 ID");
        await api.infraRecreate(service.id, service.instance_id);
        Message.success(`${service.display_name}：已重建并启动`);
        await servicesQuery.refetch();
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

  const handleAction = (service: InfraService, verb: string) => {
    if (verb !== "recreate") {
      void executeAction(service, verb);
      return;
    }
    Modal.confirm({
      title: "重建并启动 NapCat？",
      content:
        "将停止并替换当前容器，改用仓库固定的 NapCat 镜像。QQ 数据卷和 NapCat 配置卷会保留；镜像准备失败时不会停止旧容器。是否继续？",
      okText: "确认重建",
      cancelText: "取消",
      okButtonProps: { status: "danger" },
      onOk: () => executeAction(service, verb),
    });
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
        title="服务管理"
        description="服务状态、实例关联与运维操作"
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
        <div className="obs-toolbar"><div className="run-layer-filter" aria-label="服务类型">{["", "channel", "capability"].map((id) => <button key={id} type="button" aria-pressed={layer === id} className={layer === id ? "is-selected" : ""} onClick={() => setLayer(id)}>{id === "channel" ? "平台接入" : id === "capability" ? "工具服务" : "全部服务"}<small>{servicesData.filter((service) => !id || serviceLayer(service) === id).length}</small></button>)}</div>
          <Button loading={servicesQuery.isFetching} onClick={() => void servicesQuery.refetch()}>刷新状态</Button></div>
        {servicesQuery.isLoading && <Spin tip="读取服务状态…" />}
        {servicesQuery.error && <Alert type="error" content={`服务状态读取失败：${String(servicesQuery.error)}${servicesData.length ? "；下方为上次快照。" : ""}`} />}
        {!servicesQuery.isLoading && !servicesData.filter((service) => !layer || serviceLayer(service) === layer).length && <Empty description="当前分类没有服务" />}
        <div className="obs-table-scroll"><table className="obs-table"><thead><tr><th>服务</th><th>类型</th><th>状态</th><th>版本 / 容器</th><th>关联实例</th><th>运行时长</th><th>操作</th></tr></thead>
          <tbody>{servicesData.filter((service) => !layer || serviceLayer(service) === layer).map((service) => <tr key={service.id} id={`infra-${service.id}`}>
            <td><button className="obs-link" onClick={() => setSelectedService(service.id)}>{service.display_name}</button><small>{service.id}</small></td>
            <td>{service.service_type === "standalone" ? "平台接入" : "工具服务"}</td>
            <td><Tag color={healthTagColor(service.color)}>{infraStateLabel(service.state)}</Tag></td>
            <td>{String(service.extra?.version ?? service.extra?.image ?? service.container ?? "未记录")}</td>
            <td>{service.instance_id ? <a href={`#bots?instance=${encodeURIComponent(service.instance_id)}&entity=${encodeURIComponent(service.service_type === "standalone" ? "channel:qq" : `mcp:${service.id}`)}`}>{service.instance_id}</a> : "共享服务"}</td>
            <td>{service.uptime_s == null ? "—" : `${Math.floor(service.uptime_s / 60)} min`}</td>
            <td><Space size="small"><Button size="small" onClick={() => setSelectedService(service.id)}>管理</Button><Button size="small" onClick={() => openLogs(service)}>日志</Button></Space></td>
          </tr>)}</tbody></table></div>
      </PageSection>

      <Drawer title="服务详情" visible={!!selectedService} width="min(780px, 96vw)" footer={null} onCancel={() => setSelectedService("")}>
        {servicesData.filter((service) => service.id === selectedService).map((service) => <div className="obs-service-details" key={service.id}>
          <ServiceCard service={service} busy={!!busy[service.id]} onAction={(verb) => handleAction(service, verb)} onLogs={() => openLogs(service)} />
        </div>)}
      </Drawer>
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
