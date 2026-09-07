import type { GatewayObservation } from "./model";
import type { Configuration, Inspection } from "./workbenchModel";

export function latestConfiguration(inspection?: Inspection): Configuration | null | undefined {
  const current = inspection?.current;
  if (!current || !inspection.loaded || inspection.loaded_stale) return current;
  const live = new Map(inspection.loaded.entities.map((entity) => [entity.id, entity]));
  const declared = new Set(current.entities.map((entity) => entity.id));
  return { ...current, entities: [
    ...current.entities.map((entity) => {
      const runtime = live.get(entity.id);
      return { ...entity, loaded: runtime?.loaded ?? null, connected: runtime?.connected ?? null,
        runtime: runtime?.runtime };
    }),
    ...inspection.loaded.entities.filter((entity) => !declared.has(entity.id))
      .map((entity) => ({ ...entity, configured: null, available: null })),
  ] };
}

export function recordedConfiguration(inspection?: Inspection, event?: GatewayObservation) {
  const snapshot = inspection?.execution;
  if (!snapshot || !event) return snapshot;
  const ids = new Set([event.entity_id, ...(event.refs ?? [])]);
  // Historical fields come only from the immutable snapshot and exact component IDs.
  return { ...snapshot, entities: snapshot.entities.filter((entity) => ids.has(entity.id)) };
}
