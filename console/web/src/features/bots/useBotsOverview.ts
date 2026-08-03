import { useCallback, useMemo } from "react";
import { useQueries, useQuery } from "@tanstack/react-query";
import { api } from "../../api";
import type { BotInstance, BotInventory, BotStatus } from "../../types";

export function useBotsOverview(visible = true) {
  const botsQuery = useQuery({
    queryKey: ["bots"],
    queryFn: api.listBots,
    enabled: visible,
  });
  const bots = botsQuery.data ?? [];

  const statusQueries = useQueries({
    queries: bots.map((bot) => ({
      queryKey: ["bot-status", bot.instance_id],
      queryFn: () => api.status(bot.instance_id),
      enabled: visible,
      refetchInterval: visible ? 15_000 : false,
    })),
  });

  const inventoryQueries = useQueries({
    queries: bots.map((bot) => ({
      queryKey: ["bot-inventory", bot.instance_id],
      queryFn: () => api.inventory(bot.instance_id),
      enabled: visible && bots.length > 0,
      refetchInterval: visible ? 30_000 : false,
    })),
  });

  const statuses = useMemo(() => {
    const next: Record<string, BotStatus> = {};
    statusQueries.forEach((query, index) => {
      const bot = bots[index];
      if (bot && query.data) next[bot.instance_id] = query.data;
    });
    return next;
  }, [bots, statusQueries]);

  const inventoryMap = useMemo(() => {
    const next: Record<string, BotInventory> = {};
    inventoryQueries.forEach((query, index) => {
      const bot = bots[index];
      if (bot && query.data) next[bot.instance_id] = query.data;
    });
    return next;
  }, [bots, inventoryQueries]);

  const refreshStatuses = useCallback(async (_list?: BotInstance[]) => {
    await Promise.all(statusQueries.map((query) => query.refetch()));
  }, [statusQueries]);

  const reloadBots = useCallback(async () => {
    const result = await botsQuery.refetch();
    return result.data ?? [] as BotInstance[];
  }, [botsQuery]);

  const runningBotCount = useMemo(
    () => bots.filter((bot) => statuses[bot.instance_id]?.running).length,
    [bots, statuses],
  );
  const deployedBotCount = useMemo(
    () => bots.filter((bot) => bot.is_deployed).length,
    [bots],
  );

  const loading = botsQuery.isLoading || statusQueries.some((query) => query.isLoading);

  return {
    bots,
    statuses,
    inventoryMap,
    loading,
    runningBotCount,
    deployedBotCount,
    reloadBots,
    refreshStatuses,
  };
}
