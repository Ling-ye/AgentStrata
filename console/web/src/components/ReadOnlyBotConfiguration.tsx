import { useQuery } from "@tanstack/react-query";
import { Alert, Button, Space } from "@arco-design/web-react";
import { api } from "../api";
import ConfigurationPane from "../features/architecture/ConfigurationPane";
import { currentInspection, latestConfiguration } from "../features/architecture/configurationModel";
import { configurationRelated, configurationViews } from "../features/architecture/configurationPresentation";

export default function ReadOnlyBotConfiguration({ instanceId, visible, infoVisible, onInfoClose }: {
  instanceId: string; visible: boolean; infoVisible: boolean; onInfoClose: () => void;
}) {
  const inspection = useQuery({ queryKey: ["inspection", instanceId],
    queryFn: ({ signal }) => api.inspection(instanceId, undefined, undefined, signal),
    enabled: visible || infoVisible, refetchInterval: visible || infoVisible ? 5000 : false });
  const data = currentInspection(inspection.data, { failed: inspection.isError });
  const config = latestConfiguration(data);
  const entities = config ? configurationViews(config) : [];
  return <>
    {visible && <Alert type="info" content="该实例由插件管理。此处只读展示配置；请通过插件管理器修改配置或管理服务。" />}
    <ConfigurationPane instanceId={instanceId} visible={visible} infoVisible={infoVisible} onInfoClose={onInfoClose}
      inspection={data} entities={entities} loading={inspection.isLoading} error={inspection.error}
      onRefresh={() => { void inspection.refetch(); }} groupActions={() => null} groupFooter={() => null}
      entityActions={(entity, navigate) => <Space wrap>{configurationRelated(entities, entity).map((item) =>
        <Button key={item.viewKey} size="small" onClick={() => navigate(item.id)}>查看 {item.name}</Button>)}</Space>} />
  </>;
}
