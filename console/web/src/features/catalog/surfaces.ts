import type { CatalogItem } from "../../types";

export type CatalogSurface = CatalogItem["surface"];

export function catalogSurface(item: CatalogItem): CatalogSurface {
  if (item.surface) return item.surface;
  if (item.kind === "prompt") return "prompts";
  if (item.kind === "subagent" || item.kind === "workflow") return "agents";
  if (item.kind === "context_source") return "context";
  return "tools";
}
