import type { Task } from "../../types";
import {
  EvaluationApiError,
  normalizeCoverage,
  normalizeEvaluation,
  parseApiProblem,
  shouldStopEvaluationStream,
  type EvaluationCaseDescriptor,
  type EvaluationCaseDetail,
  type EvaluationTrial,
  type EvaluationCaseSummary,
  type EvaluationCoverage,
  type EvaluationProfile,
  type EvaluationRecord,
  type EvaluationRequest,
  type EvaluationSuite,
} from "./model";

async function requestJson<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, {
    ...init,
    headers: {
      ...(init?.body ? { "Content-Type": "application/json" } : {}),
      ...(init?.headers ?? {}),
    },
  });
  const payload = await response.json().catch(() => null) as unknown;
  if (!response.ok) {
    throw new EvaluationApiError(
      response.status,
      parseApiProblem(payload, `${response.status} ${response.statusText}`),
    );
  }
  return payload as T;
}

function queryString(filters: Record<string, string | undefined>): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) {
    if (value) params.set(key, value);
  }
  return params.size ? `?${params.toString()}` : "";
}

function evaluationList(value: unknown): EvaluationRecord[] {
  if (!Array.isArray(value)) {
    throw new Error("评测记录响应格式无效：预期为数组");
  }
  return value.map(normalizeEvaluation);
}

function normalizeTrial(value: unknown): EvaluationTrial {
  const item =
    typeof value === "object" && value !== null && !Array.isArray(value)
      ? value as Record<string, unknown>
      : {};
  const nullableNumber = (input: unknown) =>
    typeof input === "number" && Number.isFinite(input) ? input : null;
  return {
    trial_id: typeof item.trial_id === "string" ? item.trial_id : "",
    case_ref: typeof item.case_ref === "string" ? item.case_ref : "",
    case_id: typeof item.case_id === "string" ? item.case_id : "",
    dimension: typeof item.dimension === "string" ? item.dimension : "",
    target_id: typeof item.target_id === "string" ? item.target_id : "",
    target_fingerprint:
      typeof item.target_fingerprint === "string"
        ? item.target_fingerprint
        : "",
    attempt: typeof item.attempt === "number" ? item.attempt : 0,
    outcome:
      typeof item.outcome === "string"
        ? item.outcome
        : "",
    score: nullableNumber(item.score),
    max_score: nullableNumber(item.max_score),
    passed: typeof item.passed === "boolean" ? item.passed : null,
    duration_seconds: nullableNumber(item.duration_seconds),
    final_text: typeof item.final_text === "string" ? item.final_text : "",
    stop_reason: typeof item.stop_reason === "string" ? item.stop_reason : "",
    judge:
      typeof item.judge === "object" &&
      item.judge !== null &&
      !Array.isArray(item.judge)
        ? item.judge as Record<string, unknown>
        : null,
    events: Array.isArray(item.events)
      ? item.events.filter(
          (event): event is Record<string, unknown> =>
            typeof event === "object" && event !== null && !Array.isArray(event),
        )
      : [],
    evidence:
      typeof item.evidence === "object" &&
      item.evidence !== null &&
      !Array.isArray(item.evidence)
        ? item.evidence as Record<string, unknown>
        : {},
    error: typeof item.error === "string" ? item.error : "",
  };
}

export const evaluationApi = {
  profiles: () => requestJson<EvaluationProfile[]>("/api/evals/profiles"),

  suites: (botId: string) =>
    requestJson<EvaluationSuite[]>(
      `/api/evals/suites${queryString({ bot_id: botId })}`,
    ),

  cases: async (suiteId: string, botId: string) => {
    const response = await requestJson<
      EvaluationCaseSummary[] | { cases: EvaluationCaseSummary[] }
    >(
      `/api/evals/suites/${encodeURIComponent(suiteId)}/cases${queryString({
        bot_id: botId,
      })}`,
    );
    return Array.isArray(response) ? response : response.cases;
  },

  caseDescriptor: (suiteId: string, caseId: string, botId: string) =>
    requestJson<EvaluationCaseDescriptor>(
      `/api/evals/suites/${encodeURIComponent(suiteId)}/cases/${encodeURIComponent(caseId)}${queryString({
        bot_id: botId,
      })}`,
    ),

  prepareSuite: (suiteId: string, botId: string) =>
    requestJson<Task>(
      `/api/evals/suites/${encodeURIComponent(suiteId)}/prepare${queryString({
        bot_id: botId,
      })}`,
      { method: "POST" },
    ),

  coverage: async (botId: string) =>
    normalizeCoverage(
      await requestJson<EvaluationCoverage>(
        `/api/evals/cases/coverage${queryString({ bot_id: botId })}`,
      ),
    ),

  create: async (request: EvaluationRequest) =>
    normalizeEvaluation(
      await requestJson<unknown>("/api/evals/evaluations", {
        method: "POST",
        body: JSON.stringify(request),
      }),
    ),

  list: async (filters: {
    kind?: string;
    bot_id?: string;
    status?: string;
  } = {}) =>
    evaluationList(
      await requestJson<unknown>(
        `/api/evals/evaluations${queryString(filters)}`,
      ),
    ),

  get: async (evaluationId: string) =>
    normalizeEvaluation(
      await requestJson<unknown>(
        `/api/evals/evaluations/${encodeURIComponent(evaluationId)}`,
      ),
    ),

  caseDetail: async (evaluationId: string, caseRef: string) => {
    const response = await requestJson<{
      case_ref?: string;
      comparison?: Record<string, unknown> | null;
      trials?: unknown[];
    }>(
      `/api/evals/evaluations/${encodeURIComponent(evaluationId)}/cases/${encodeURIComponent(caseRef)}`,
    );
    return {
      case_ref: response.case_ref ?? caseRef,
      comparison: response.comparison ?? null,
      trials: (response.trials ?? []).map(normalizeTrial),
    } satisfies EvaluationCaseDetail;
  },

  cancel: async (evaluationId: string) =>
    normalizeEvaluation(
      await requestJson<unknown>(
        `/api/evals/evaluations/${encodeURIComponent(evaluationId)}/cancel`,
        { method: "POST" },
      ),
    ),

  rerun: async (evaluationId: string) =>
    normalizeEvaluation(
      await requestJson<unknown>(
        `/api/evals/evaluations/${encodeURIComponent(evaluationId)}/rerun`,
        { method: "POST" },
      ),
    ),

  remove: (evaluationId: string) =>
    requestJson<{ ok: boolean }>(
      `/api/evals/evaluations/${encodeURIComponent(evaluationId)}`,
      { method: "DELETE" },
    ),
};

export function streamEvaluation(
  evaluationId: string,
  onEvent: (event: Record<string, unknown>) => void,
  onEnd: () => void,
): () => void {
  const source = new EventSource(
    `/api/evals/evaluations/${encodeURIComponent(evaluationId)}/stream`,
  );
  let consecutiveErrors = 0;
  let finished = false;
  const finish = () => {
    if (finished) return;
    finished = true;
    source.close();
    onEnd();
  };
  source.onopen = () => {
    consecutiveErrors = 0;
  };
  source.onmessage = (message) => {
    try {
      const value = JSON.parse(message.data) as unknown;
      if (typeof value === "object" && value !== null && !Array.isArray(value)) {
        onEvent(value as Record<string, unknown>);
      }
    } catch {
      // Persisted Evaluation state remains authoritative.
    }
  };
  source.addEventListener("end", () => {
    finish();
  });
  source.onerror = () => {
    consecutiveErrors += 1;
    if (shouldStopEvaluationStream(consecutiveErrors, source.readyState)) {
      finish();
    }
  };
  return () => {
    finished = true;
    source.close();
  };
}

export function evaluationExportUrl(
  evaluationId: string,
  format: "json" | "markdown",
): string {
  return `/api/evals/evaluations/${encodeURIComponent(evaluationId)}/export/${format}`;
}
