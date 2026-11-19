import { applicationHref } from "./navigation";
import { Alert, Button, Space, Spin } from "@arco-design/web-react";
import { useQuery } from "@tanstack/react-query";
import type { BotInstance } from "../../types";
import ObservationWorkbench from "../architecture/ObservationWorkbench";
import type { ApplicationView } from "./api";
import { managementRequest } from "./settingsApi";

export default function ApplicationRecords({ application, visible }: { application: ApplicationView; visible: boolean }) {
  const source = useQuery({ queryKey: ["application-observation-source", application.id],
    queryFn: ({ signal }) => managementRequest<BotInstance>(application.id, "/observations/source", { signal }), enabled: visible });
  if (source.isPending) return <Spin />;
  if (source.isError) return <Alert type="error" content={source.error.message} action={<Space><Button onClick={() => void source.refetch()}>重试</Button>
    <Button href={application.source === "native" ? "#connect" : applicationHref(application.id, "runtime")}>连接运行端</Button></Space>} />;
  if (source.data.runtime_kind !== "gateway") return <Button href={application.links.records}>打开此应用的历史记录</Button>;
  return <ObservationWorkbench key={application.id} bot={{ ...source.data, instance_id: "app:" + application.id }} applicationId={application.id} visible={visible} />;
}
