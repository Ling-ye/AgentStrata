import type { BotTask, TaskTool } from "../../types";

const pad2 = (n: number) => String(n).padStart(2, "0");

export function fmtClock(ms: number | null | undefined): string {
  if (ms == null || !Number.isFinite(ms)) return "-";
  const d = new Date(ms);
  return `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`;
}

export function fmtTime(v: number | string | null | undefined): string {
  const sec = typeof v === "string" ? Number(v) : v;
  if (sec == null || !Number.isFinite(sec) || sec <= 0) return "-";
  const d = new Date(sec * 1000);
  return (
    `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ` +
    `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`
  );
}

export function fmtElapsed(s: number | null | undefined): string {
  if (s == null || !Number.isFinite(s) || s < 0) return "-";
  if (s < 60) return `${Math.round(s)}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}

export function fmtInt(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return "-";
  return Math.round(v).toLocaleString();
}

export function fmtPercent(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return "-";
  return `${Math.round(v * 1000) / 10}%`;
}

export function jobStatusColor(status: string) {
  if (status === "running") return "blue";
  if (status === "delegated") return "purple";
  if (status === "succeeded") return "green";
  if (status === "failed") return "red";
  if (status === "queued") return "orange";
  return "grey";
}

export function formatTaskSubmitter(task: BotTask): string {
  const name = task.workspace?.user_name || task.submitter || "";
  const account = task.workspace?.user_id || "";
  if (name && account && name !== account) {
    return `${name}（${account}）`;
  }
  return name || account || "-";
}

export function formatTools(tools: TaskTool[] | undefined): string {
  if (!tools || tools.length === 0) return "未调用工具";
  return tools
    .map((tool) => {
      const elapsed = fmtElapsed(tool.elapsed_s);
      return `${tool.name} · ${tool.status}${elapsed === "-" ? "" : ` · ${elapsed}`}`;
    })
    .join("\n");
}

export function formatUsageDetail(task: BotTask): string {
  const usage = task.usage_totals;
  if (!usage || !usage.llm_calls) return "暂无模型用量数据";
  return [
    `模型调用：${fmtInt(usage.llm_calls)}`,
    `总 Token：${fmtInt(usage.total_tokens)}`,
    `输入 Token：${fmtInt(usage.prompt_tokens)}`,
    `输出 Token：${fmtInt(usage.completion_tokens)}`,
    `Cached Token：${fmtInt(usage.cached_tokens)}`,
    `Cache Read Token：${fmtInt(usage.cache_read_tokens)}`,
    `Cache Write Token：${fmtInt(usage.cache_write_tokens)}`,
    `Token 命中率：${fmtPercent(usage.cache_hit_rate)}（cached_tokens / prompt_tokens）`,
    `命中调用：${fmtInt(usage.cache_hit_calls)} / ${fmtInt(usage.llm_calls)}（${fmtPercent(
      usage.cache_hit_call_rate,
    )}）`,
  ].join("\n");
}
