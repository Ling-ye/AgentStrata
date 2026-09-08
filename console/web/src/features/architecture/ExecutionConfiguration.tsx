import { useQuery } from "@tanstack/react-query";
import { Alert, Button, Spin } from "@arco-design/web-react";
import { api } from "../../api";
import type { GatewayObservation } from "./model";
import { recordedConfiguration } from "./configurationModel";
import { ConfigFields, DetailScope, FIELD_NAMES, useInView } from "./ObservationContent";

export default function ExecutionConfiguration({ instanceId, runId, event, active }: {
  instanceId: string; runId: string; event?: GatewayObservation; active: boolean;
}) {
  const { ref, visible } = useInView();
  const query = useQuery({ queryKey: ["execution-configuration", instanceId, runId, event?.seq],
    queryFn: () => api.inspection(instanceId, runId, event?.seq),
    enabled: active && visible, staleTime: Infinity, retry: false });
  const config = recordedConfiguration(query.data, event);
  return <section ref={ref} className="obs-execution-config" aria-label={event ? "步骤执行时配置" : "任务执行时配置"}>
    <div className="obs-pane-heading"><strong>{event ? "执行时配置" : "任务配置记录"}</strong>
      {config && <span>版本 {config.configuration_revision?.slice(0, 10) ?? "未记录"}</span>}</div>
    {query.error ? <Alert type="error" content={<span>配置记录读取失败 <Button size="mini" onClick={() => void query.refetch()}>重试</Button></span>} /> :
      query.isFetching ? <Spin size={16} /> : !query.data ? <p className="obs-muted">滚动到此处时加载</p> :
        !config ? <p className="obs-muted">此任务未记录执行时配置</p> : !config.entities.length ? <p className="obs-muted">此步骤未记录关联组件的配置</p> :
          <>{config.visibility !== "operator" && <p className="obs-muted">此历史快照未保留完整配置原值，已省略的内容无法恢复。</p>}
          {(config.capture_state === "truncated" || query.data.sanitization_truncated) && <p className="obs-muted">配置记录已截断，当前仅显示已取得的字段。</p>}
          {config.entities.map((entity) => <DetailScope id={`config:${entity.id}`} key={entity.id}><div className="obs-execution-config-item">
            <h4>{FIELD_NAMES[entity.name] ?? entity.name}</h4>
            <ConfigFields value={entity.config} missingLabel={config.visibility === "operator" ? "未设置" : "未记录"} />
            {entity.runtime && <DetailScope id="runtime"><h4>执行时运行参数</h4><ConfigFields value={entity.runtime} /></DetailScope>}
            {entity.environment && !!Object.keys(entity.environment).length && <DetailScope id="environment"><h4>执行时环境配置</h4><ConfigFields value={entity.environment} missingLabel={config.visibility === "operator" ? "未设置" : "未记录"} /></DetailScope>}
          </div></DetailScope>)}</>}
  </section>;
}
