import { configurationTabForEntity } from "./configurationPresentation";
import type { ObservationFilters } from "./workbenchModel";

export type BotTab = "tasks" | "configuration" | "runtime" | "capabilities";
export function botTabFromParams(params: URLSearchParams): BotTab {
  const tab = params.get("tab");
  if (tab === "observation" || tab === "history" || tab === "tasks") return "tasks";
  if (params.has("entity") && (!tab || tab === "configuration" || tab === "capabilities")) return configurationTabForEntity(params.get("entity")!);
  if (tab === "configuration" || tab === "runtime" || tab === "capabilities") return tab;
  return params.has("entity") ? "configuration" : "tasks";
}

export function readSessionValue(key: string): unknown {
  try { return JSON.parse(sessionStorage.getItem(key) || "null"); } catch { return null; }
}
export function saveSessionValue(key: string, value: unknown) {
  try { sessionStorage.setItem(key, JSON.stringify(value)); } catch { /* Browser storage can be disabled. */ }
}

export function taskWorkspaceState(instanceId: string, params: URLSearchParams, saved: unknown, now = Date.now() / 1000) {
  const record = saved && typeof saved === "object" ? saved as Record<string, unknown> : {};
  const range = ["1", "7", "30", "custom"].includes(String(record.range)) ? String(record.range) : "1";
  const raw = record.filters && typeof record.filters === "object" ? record.filters as Record<string, unknown> : {};
  const filters: ObservationFilters = {};
  for (const key of ["state", "config_id", "backend", "model", "component", "error_code", "search"] as const) {
    if (typeof raw[key] === "string") filters[key] = raw[key].slice(0, 256);
  }
  for (const key of ["since", "until", "min_ms", "page"] as const) {
    if (typeof raw[key] === "number" && Number.isFinite(raw[key]) && raw[key] >= 0) filters[key] = raw[key];
  }
  filters.page = Math.max(1, Math.min(100000, Math.floor(filters.page ?? 1)));
  if (range !== "custom") { filters.since = now - Number(range) * 86400; delete filters.until; }
  const linked = !params.get("instance") || params.get("instance") === instanceId;
  return {
    selected: linked && params.get("run") || (typeof record.selected === "string" ? record.selected : ""), filters, range,
    customStart: typeof record.customStart === "string" ? record.customStart : "",
    customEnd: typeof record.customEnd === "string" ? record.customEnd : "",
  };
}

export interface ScrollBookmark { top: number; anchor?: string; offset: number; pages?: number }
export function scrollBookmark(value: unknown): ScrollBookmark {
  const saved = value && typeof value === "object" ? value as Record<string, unknown> : {};
  return {
    top: typeof saved.top === "number" && Number.isFinite(saved.top) ? Math.max(0, saved.top) : 0,
    anchor: typeof saved.anchor === "string" ? saved.anchor : undefined,
    offset: typeof saved.offset === "number" && Number.isFinite(saved.offset) ? saved.offset : 0,
    pages: typeof saved.pages === "number" && Number.isFinite(saved.pages) ? Math.max(1, Math.min(1000, saved.pages)) : 1,
  };
}
