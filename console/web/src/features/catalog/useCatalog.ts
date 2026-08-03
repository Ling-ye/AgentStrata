import { useQuery } from "@tanstack/react-query";
import { api } from "../../api";
import type { BotToolConfig, CatalogItem } from "../../types";

export function useCatalog() {
  return useQuery<CatalogItem[]>({
    queryKey: ["catalog"],
    queryFn: () => api.catalog(),
    staleTime: 60_000,
    retry: 1,
    refetchOnWindowFocus: false,
  });
}

export function useBotToolConfig(instanceId: string) {
  return useQuery<BotToolConfig>({
    queryKey: ["bot-tools", instanceId],
    queryFn: () => api.botTools(instanceId),
    staleTime: 10_000,
    retry: 1,
    refetchOnWindowFocus: false,
  });
}
