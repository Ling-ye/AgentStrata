import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import type { GatewayObservation, GatewayRun, GatewayRunDetail } from "./model";
import type { ObservationBody } from "./workbenchModel";

export function mergeObservationEvents(...pages: GatewayObservation[][]): GatewayObservation[] {
  return [...new Map(pages.flat().map((event) => [event.seq, event])).values()].sort((a, b) => a.seq - b.seq);
}

export function useObservationStream(instanceId: string, runId: string | null, active: boolean, after: number) {
  const client = useQueryClient();
  const initialCursor = useRef(after);
  initialCursor.current = after;
  const [state, setState] = useState<{ key: string; events: GatewayObservation[]; status: string }>({ key: "", events: [], status: "" });
  const key = `${instanceId}:${runId}`;
  useEffect(() => {
    if (!active || !runId) return;
    let source: EventSource | undefined, retry = 0, disposed = false;
    let cursor = initialCursor.current;
    const events = new Map<number, GatewayObservation>();
    const status = (value: string) => { if (!disposed) setState({ key, events: [...events.values()], status: value }); };
    const open = () => {
      if (disposed || document.hidden) return;
      source = new EventSource(`/api/bots/${encodeURIComponent(instanceId)}/gateway-observation/runs/${encodeURIComponent(runId)}/stream?after=${cursor}`);
      source.onopen = () => status("已连接实时过程");
      source.onerror = () => status("实时连接中断，正在续读；机器人继续执行");
      source.addEventListener("observation", (raw) => {
        if (disposed) return;
        try {
          const frame = JSON.parse((raw as MessageEvent).data) as { observation: GatewayObservation; body?: ObservationBody };
          const event = frame.observation;
          if (!event || !Number.isSafeInteger(event.seq) || event.seq <= cursor) return;
          if (frame.body && event.body_ref) client.setQueryData(["observation-body", instanceId, runId, event.body_ref], frame.body);
          cursor = event.seq;
          events.set(event.seq, event);
          status("已连接实时过程");
        } catch { status("实时记录格式异常，等待重新连接"); }
      });
      source.addEventListener("run", (raw) => {
        if (disposed) return;
        try {
          const run = JSON.parse((raw as MessageEvent).data) as GatewayRun;
          if (run.run_id === runId) client.setQueryData<GatewayRunDetail>(["observation-run", instanceId, runId], (previous) => previous ? { ...previous, run } : previous);
        } catch { status("任务状态暂不可用"); }
      });
      source.addEventListener("unavailable", () => {
        source?.close(); status("过程读取暂不可用，正在重连");
        retry = window.setTimeout(open, 2000);
      });
    };
    const visibility = () => {
      source?.close(); window.clearTimeout(retry);
      if (!document.hidden) open();
    };
    open();
    document.addEventListener("visibilitychange", visibility);
    return () => { disposed = true; source?.close(); window.clearTimeout(retry); document.removeEventListener("visibilitychange", visibility); };
  }, [instanceId, runId, active, key, client]);
  return state.key === key ? state : { events: [], status: "" };
}
