import type { BotTask, LlmCallUsage, LlmUsageTotals } from "./types";

type UsageLike = Partial<LlmUsageTotals> | undefined;

interface ModelPrice {
  label: string;
  inputUncachedRmbPer1m: number;
  inputCachedRmbPer1m: number;
  outputRmbPer1m: number;
}

interface CostPart {
  model: string;
  label: string;
  calls: number;
  promptTokens: number;
  cachedTokens: number;
  uncachedTokens: number;
  completionTokens: number;
  reasoningTokens: number;
  estimatedRmb: number;
}

export interface CostEstimate {
  status: "none" | "estimated" | "unpriced";
  estimatedRmb: number;
  parts: CostPart[];
  unpricedModels: string[];
  source: "llm_calls" | "usage_totals" | "none";
}

export interface ModelSummary {
  text: string;
  detail: string;
}

const MODEL_PRICES: Record<string, ModelPrice> = {
  "deepseek-v4-pro": {
    label: "DeepSeek V4 Pro",
    inputUncachedRmbPer1m: 3.0,
    inputCachedRmbPer1m: 0.025,
    outputRmbPer1m: 6.0,
  },
  "deepseek-v4-flash": {
    label: "DeepSeek V4 Flash",
    inputUncachedRmbPer1m: 1.0,
    inputCachedRmbPer1m: 0.02,
    outputRmbPer1m: 2.0,
  },
};

const MODEL_ALIASES: Record<string, string> = {
  "deepseek-v4": "deepseek-v4-pro",
  "deepseek_v4_pro": "deepseek-v4-pro",
  "deepseek-v4-pro": "deepseek-v4-pro",
  "deepseek-v4-flash": "deepseek-v4-flash",
  "deepseek_v4_flash": "deepseek-v4-flash",
  "deepseek-chat": "deepseek-v4-flash",
  "deepseek-reasoner": "deepseek-v4-flash",
};

function normalizeModel(model: string | undefined): string {
  const raw = (model || "").trim().toLowerCase();
  if (!raw) return "";
  return MODEL_ALIASES[raw] || raw;
}

function usageNumber(usage: UsageLike, key: keyof LlmUsageTotals): number {
  const value = usage?.[key];
  return typeof value === "number" && Number.isFinite(value) ? Math.max(Math.round(value), 0) : 0;
}

function cachedInputTokens(usage: UsageLike): number {
  const promptTokens = usageNumber(usage, "prompt_tokens");
  const cachedTokens = usageNumber(usage, "cached_tokens") || usageNumber(usage, "cache_read_tokens");
  return Math.min(cachedTokens, promptTokens);
}

function estimatePart(model: string, usage: UsageLike, calls: number): CostPart | null {
  const normalizedModel = normalizeModel(model);
  const price = normalizedModel ? MODEL_PRICES[normalizedModel] : undefined;
  if (!price) return null;
  const promptTokens = usageNumber(usage, "prompt_tokens");
  const completionTokens = usageNumber(usage, "completion_tokens");
  const reasoningTokens = usageNumber(usage, "reasoning_tokens");
  const cachedTokens = cachedInputTokens(usage);
  const uncachedTokens = Math.max(promptTokens - cachedTokens, 0);
  const estimatedRmb =
    (uncachedTokens / 1_000_000) * price.inputUncachedRmbPer1m +
    (cachedTokens / 1_000_000) * price.inputCachedRmbPer1m +
    (completionTokens / 1_000_000) * price.outputRmbPer1m;
  return {
    model: normalizedModel,
    label: price.label,
    calls,
    promptTokens,
    cachedTokens,
    uncachedTokens,
    completionTokens,
    reasoningTokens,
    estimatedRmb,
  };
}

function mergeUsage(a: UsageLike, b: UsageLike): Partial<LlmUsageTotals> {
  return {
    prompt_tokens: usageNumber(a, "prompt_tokens") + usageNumber(b, "prompt_tokens"),
    completion_tokens: usageNumber(a, "completion_tokens") + usageNumber(b, "completion_tokens"),
    total_tokens: usageNumber(a, "total_tokens") + usageNumber(b, "total_tokens"),
    reasoning_tokens: usageNumber(a, "reasoning_tokens") + usageNumber(b, "reasoning_tokens"),
    cached_tokens: usageNumber(a, "cached_tokens") + usageNumber(b, "cached_tokens"),
    cache_read_tokens: usageNumber(a, "cache_read_tokens") + usageNumber(b, "cache_read_tokens"),
    cache_write_tokens: usageNumber(a, "cache_write_tokens") + usageNumber(b, "cache_write_tokens"),
    llm_calls: usageNumber(a, "llm_calls") + usageNumber(b, "llm_calls"),
  };
}

export function summarizeTaskModels(task: BotTask): ModelSummary {
  const counts = new Map<string, number>();
  for (const call of task.llm_calls || []) {
    const model = (call.model || "").trim();
    if (!model) continue;
    counts.set(model, (counts.get(model) || 0) + 1);
  }
  const entries = [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
  if (entries.length === 0) return { text: "-", detail: "暂无模型调用数据" };
  const [primaryModel, primaryCalls] = entries[0];
  const text = entries.length === 1 ? primaryModel : `${primaryModel} +${entries.length - 1}`;
  const detail = entries.map(([model, calls]) => `${model}：${calls} 次`).join("\n");
  return {
    text: entries.length === 1 ? `${primaryModel} (${primaryCalls})` : text,
    detail,
  };
}

export function estimateTaskCost(task: BotTask): CostEstimate {
  const calls = task.llm_calls || [];
  const byModel = new Map<string, { usage: Partial<LlmUsageTotals>; calls: number; rawModel: string }>();
  for (const call of calls) {
    const rawModel = (call.model || "").trim();
    if (!rawModel || !call.usage) continue;
    const key = normalizeModel(rawModel) || rawModel;
    const prev = byModel.get(key);
    byModel.set(key, {
      rawModel,
      calls: (prev?.calls || 0) + 1,
      usage: mergeUsage(prev?.usage, call.usage),
    });
  }

  const parts: CostPart[] = [];
  const unpricedModels: string[] = [];
  for (const item of byModel.values()) {
    const part = estimatePart(item.rawModel, item.usage, item.calls);
    if (part) {
      parts.push(part);
    } else if (!unpricedModels.includes(item.rawModel)) {
      unpricedModels.push(item.rawModel);
    }
  }

  if (parts.length > 0 || unpricedModels.length > 0) {
    return {
      status: parts.length > 0 && unpricedModels.length === 0 ? "estimated" : "unpriced",
      estimatedRmb: parts.reduce((sum, part) => sum + part.estimatedRmb, 0),
      parts,
      unpricedModels,
      source: "llm_calls",
    };
  }

  const usage = task.usage_totals;
  const model = calls.find((call: LlmCallUsage) => call.model)?.model || "";
  if (!usage || !usage.llm_calls) return { status: "none", estimatedRmb: 0, parts: [], unpricedModels: [], source: "none" };
  const fallback = estimatePart(model, usage, usage.llm_calls);
  if (!fallback) {
    return {
      status: "unpriced",
      estimatedRmb: 0,
      parts: [],
      unpricedModels: model ? [model] : ["未知模型"],
      source: "usage_totals",
    };
  }
  return {
    status: "estimated",
    estimatedRmb: fallback.estimatedRmb,
    parts: [fallback],
    unpricedModels: [],
    source: "usage_totals",
  };
}

export function formatRmb(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return "¥0.000000";
  return `¥${value.toFixed(6)}`;
}

export function formatCostDetail(estimate: CostEstimate): string {
  if (estimate.status === "none") return "暂无可计费用量数据";
  const lines = [
    "费用为按本地价格表计算的任务级估算，不代表供应商实时扣费。",
    `数据来源：${estimate.source === "llm_calls" ? "逐轮 LLM 调用" : "任务用量汇总"}`,
  ];
  for (const part of estimate.parts) {
    const visibleTokens = part.reasoningTokens > 0
      ? part.completionTokens - part.reasoningTokens
      : part.completionTokens;
    lines.push(
      "",
      `${part.label}（${part.model}）`,
      `调用：${part.calls} 次`,
      `未命中输入 Token：${part.uncachedTokens.toLocaleString()}`,
      `Cache 命中输入 Token：${part.cachedTokens.toLocaleString()}`,
      `输出 Token：${part.completionTokens.toLocaleString()}` +
        (part.reasoningTokens > 0
          ? `（思考 ${part.reasoningTokens.toLocaleString()} + 可见 ${Math.max(visibleTokens, 0).toLocaleString()}）`
          : ""),
      `估算费用：${formatRmb(part.estimatedRmb)}`,
    );
  }
  if (estimate.unpricedModels.length) {
    lines.push("", `未配置价格模型：${estimate.unpricedModels.join("、")}`);
  }
  return lines.join("\n");
}
