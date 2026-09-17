import type { GatewayObservation } from "./model";
import type { Configuration, Inspection } from "./workbenchModel";

export function currentInspection(inspection: Inspection | undefined, options: { running?: boolean; failed?: boolean; applying?: boolean }, now = Date.now() / 1000): Inspection | undefined {
  if (!inspection) return inspection;
  const expired = !!inspection.loaded_meta && now - inspection.loaded_meta.observed_at > 15;
  if (options.running !== false && !options.failed && !options.applying && !expired && !inspection.loaded_stale) return inspection;
  return { ...inspection, loaded_stale: true, configuration_status: "unknown",
    configuration_status_reason: options.applying ? "配置应用任务执行中，等待最终结果和新的运行观测。" : options.running === false ?
      "服务已停止，运行状态暂无法确认。" : "运行快照过期或读取失败，暂无法确认应用状态。" };
}

export function latestConfiguration(inspection?: Inspection): Configuration | null | undefined {
  const current = inspection?.current;
  if (!current || !inspection.loaded) return current;
  const live = new Map(inspection.loaded.entities.map((entity) => [entity.id, entity]));
  const declared = new Set(current.entities.map((entity) => entity.id));
  return { ...current, entities: [
    ...current.entities.map((entity) => {
      const runtime = live.get(entity.id);
      return { ...entity, loaded: inspection.loaded_stale ? null : runtime?.loaded ?? null, connected: inspection.loaded_stale ? null : runtime?.connected ?? null,
        runtime: inspection.loaded_stale ? undefined : runtime?.runtime };
    }),
    ...inspection.loaded.entities.filter((entity) => !declared.has(entity.id))
      .map((entity) => ({ ...entity, configured: null, available: null,
        ...(inspection.loaded_stale ? { loaded: null, connected: null, runtime: undefined, runtime_stale: true } : {}) })),
  ] };
}

export function recordedConfiguration(inspection?: Inspection, event?: GatewayObservation) {
  const snapshot = inspection?.execution;
  if (!snapshot || !event) return snapshot;
  const ids = new Set([event.entity_id, ...(event.refs ?? [])]);
  // Historical fields come only from the immutable snapshot and exact component IDs.
  return { ...snapshot, entities: snapshot.entities.filter((entity) => ids.has(entity.id)) };
}
