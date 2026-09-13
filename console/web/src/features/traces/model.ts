export interface TraceSpan {
  id: string; parent_id?: string; name: string; type: string; status: string;
  start_time: string; end_time?: string; model?: string; order: number; coverage?: string;
}
export interface TracePage {
  capture_state: string; execution_status?: string; capture_reasons?: string[];
  spans: TraceSpan[]; span_count?: number; next_cursor: number; has_more: boolean;
}
export const captureLabels: Record<string, string> = {
  available: "归档可用", partial: "部分采集", recording: "运行结束后归档",
  failed: "归档失败", expired: "正文已过期", not_recorded: "未记录本地归档",
};
export function spanDepth(span: TraceSpan, spans: TraceSpan[]): number {
  const byId = new Map(spans.map(item => [item.id, item]));
  const seen = new Set([span.id]);
  let parent = span.parent_id, depth = 0;
  while (parent && byId.has(parent) && !seen.has(parent)) {
    seen.add(parent); depth++; parent = byId.get(parent)?.parent_id;
  }
  return Math.min(depth, 6);
}
export function filterSpans(spans: TraceSpan[], text: string, type: string, errors: boolean): TraceSpan[] {
  return spans.filter(span => (!type || span.type === type) &&
    (!errors || ["failed", "error", "incomplete", "cancelled", "aborted"].includes(span.status)) &&
    `${span.name} ${span.model ?? ""}`.toLowerCase().includes(text.toLowerCase()));
}
