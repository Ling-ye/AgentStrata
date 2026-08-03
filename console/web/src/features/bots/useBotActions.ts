import { useCallback } from "react";
import { Message } from "@arco-design/web-react";
import { api } from "../../api";
import { useBusyMap } from "../../shared/hooks/useBusyMap";
import type { BotInstance, Task } from "../../types";

export type ActionVerb = "start" | "stop" | "restart" | "update" | "dump";

interface Options {
  bots: BotInstance[];
  refreshStatuses: (bots: BotInstance[]) => void | Promise<void>;
  openTask: (
    bot: BotInstance,
    kind: string,
    task: Task,
    options?: { resolveFinalStatus?: boolean },
  ) => void;
}

export function useBotActions({ bots, refreshStatuses, openTask }: Options) {
  const { busy, isBusy, setBusyFor } = useBusyMap();

  const handleAction = useCallback(
    async (bot: BotInstance, verb: ActionVerb) => {
      setBusyFor(bot.instance_id, true);
      try {
        if (verb === "start" || verb === "stop" || verb === "restart") {
          await api.control(bot.instance_id, verb);
          Message.success(`${bot.display_name}：${verb} 成功`);
          await refreshStatuses(bots);
        } else {
          const task =
            verb === "update"
              ? await api.update(bot.instance_id)
              : await api.dump(bot.instance_id);
          const label = verb === "update" ? "更新并重启" : "诊断快照";
          openTask(bot, label, task, { resolveFinalStatus: verb === "update" });
        }
      } catch (e) {
        Message.error(`${bot.display_name}：${e instanceof Error ? e.message : String(e)}`);
      } finally {
        setBusyFor(bot.instance_id, false);
      }
    },
    [bots, openTask, refreshStatuses, setBusyFor],
  );

  const handleRegister = useCallback(
    async (bot: BotInstance) => {
      setBusyFor(bot.instance_id, true);
      try {
        const task = await api.register(bot.instance_id);
        openTask(bot, "注册服务", task);
      } catch (e) {
        Message.error(`${bot.display_name}：${e instanceof Error ? e.message : String(e)}`);
      } finally {
        setBusyFor(bot.instance_id, false);
      }
    },
    [openTask, setBusyFor],
  );

  return { busy, isBusy, handleAction, handleRegister };
}
