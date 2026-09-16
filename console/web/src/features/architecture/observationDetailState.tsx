import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { readSessionValue, saveSessionValue } from "./taskWorkspaceState";

type DetailValue = boolean | number;
const DetailState = createContext<{ prefix: string; values: Record<string, DetailValue>; set: (key: string, value: DetailValue) => void } | null>(null);

export function TaskDetailState({ instanceId, runId, children }: { instanceId: string; runId: string; children: ReactNode }) {
  const storageKey = `obs:details:${instanceId}:${runId}`;
  const [values, setValues] = useState<Record<string, DetailValue>>(() => {
    const saved = readSessionValue(storageKey);
    return saved && typeof saved === "object" && !Array.isArray(saved) ? Object.fromEntries(Object.entries(saved)
      .filter(([, value]) => typeof value === "boolean" || typeof value === "number" && Number.isFinite(value) && value >= 0)) : {};
  });
  useEffect(() => saveSessionValue(storageKey, values), [storageKey, values]);
  return <DetailState.Provider value={{ prefix: "task", values,
    set: (key, value) => setValues((current) => current[key] === value ? current : { ...current, [key]: value }) }}>{children}</DetailState.Provider>;
}

export function DetailScope({ id, children }: { id: string; children: ReactNode }) {
  const state = useContext(DetailState);
  return state ? <DetailState.Provider value={{ ...state, prefix: state.prefix + "/" + JSON.stringify(id) }}>{children}</DetailState.Provider> : <>{children}</>;
}

export function useDetailValue<T extends DetailValue>(key: string, initial: T): [T, (value: T) => void] {
  const state = useContext(DetailState);
  const [local, setLocal] = useState<T>(initial);
  const fullKey = state?.prefix + "/" + key;
  const saved = state?.values[fullKey];
  return [state && typeof saved === typeof initial ? saved as T : state ? initial : local,
    (value) => { if (state) state.set(fullKey, value); else setLocal(value); }];
}

