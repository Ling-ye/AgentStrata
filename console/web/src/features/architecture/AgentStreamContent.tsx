import { useQueries } from "@tanstack/react-query";
import { api } from "../../api";
import type { GatewayObservation } from "./model";
import type { ObservationBody } from "./workbenchModel";
import { TextPreview } from "./ObservationContent";

export function deltaContent(events: GatewayObservation[], bodies: Array<ObservationBody | undefined>) {
  const sections = new Map<number, string>();
  const revisions = new Set<number>();
  let gap = false, truncated = false;
  events.forEach((event, index) => {
    const revision = Number(event.data?.revision);
    if (revisions.has(revision)) return;
    revisions.add(revision);
    const body = bodies[index];
    truncated ||= body?.state === "truncated" || event.body_state === "truncated";
    const payload = body?.payload as { delta?: string; section?: number } | undefined;
    if (!payload || typeof payload.delta !== "string") { gap = true; return; }
    const section = payload.section ?? 0;
    sections.set(section, (sections.get(section) ?? "") + payload.delta);
  });
  return { text: [...sections].sort(([a], [b]) => a - b).map(([, text]) => text).join("\n\n"), gap, truncated };
}

export default function AgentStreamContent({ events, instanceId, runId, expired }: {
  events: GatewayObservation[]; instanceId: string; runId: string; expired: boolean;
}) {
  const queries = useQueries({ queries: events.map((event) => ({
    queryKey: ["observation-body", instanceId, runId, event.body_ref],
    queryFn: ({ signal }: { signal: AbortSignal }) => api.observationBody(instanceId, runId, event.body_ref!, signal),
    enabled: !!event.body_ref && !expired, staleTime: Infinity, retry: false,
  })) });
  if (expired) return <p className="obs-muted">详细正文已到期。</p>;
  const content = deltaContent(events, queries.map((query) => query.data));
  return <section className="obs-stream-content" aria-label="实时过程正文">
    {content.text && <TextPreview text={content.text} />}
    {content.truncated && <p className="obs-muted">内容已达采集上限，后续正文未保留。</p>}
    {content.gap && <p className="obs-muted">{queries.some((query) => query.isFetching) ? "正在读取此前过程…" : "部分过程正文未取得或采集失败。"}</p>}
    {!content.text && !content.gap && <p className="obs-muted">正在等待公开过程事件…</p>}
  </section>;
}
