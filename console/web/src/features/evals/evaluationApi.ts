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

const object = (v: unknown): Record<string, unknown> => v !== null && typeof v === "object" && !Array.isArray(v) ? v as Record<string, unknown> : {};

export function normalizeTrial(value: unknown): EvaluationTrial {
  const item =
    typeof value === "object" && value !== null && !Array.isArray(value)
      ? value as Record<string, unknown>
      : {};
  const execution = object(item.execution), assessment = object(item.assessment);
  const metadata = object(execution.metadata), judge = object(assessment.judge), failure = object(item.error);
  const expected = object(item.expectation);
  const timing = object(object(metadata.execution).timing);
  const nullableNumber = (input: unknown) =>
    typeof input === "number" && Number.isFinite(input) ? input : null;
  return {
    expectation: Object.keys(expected).length ? {
      reference_answer: expected.reference_answer, behavior: typeof expected.behavior === "string" ? expected.behavior : "",
      checks: Array.isArray(expected.checks) ? expected.checks.filter((v): v is string => typeof v === "string") : [],
      source: typeof expected.source === "string" ? expected.source : "",
    } : undefined,
    failure: typeof failure.message === "string" ? { stage: String(failure.stage), code: String(failure.code), message: failure.message } : null,
    execution_seconds: nullableNumber(timing.seconds), scoring_seconds: nullableNumber(assessment.duration_seconds),
    model_output_preview: typeof item.model_output_preview === "object" && item.model_output_preview !== null
      ? item.model_output_preview as Record<string, unknown> : undefined,
    case_instance_id: typeof item.case_instance_id === "string" ? item.case_instance_id : "",
    input_preview: typeof item.input_preview === "string" ? item.input_preview : "",
    body_available: item.body_available === true,
    capture_state: typeof item.capture_state === "string" ? item.capture_state : "",
    started_at: typeof execution.started_at === "string" ? execution.started_at : "",
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
    score: nullableNumber(judge.score),
    max_score: nullableNumber(judge.max_score),
    passed: item.outcome === "passed" ? true : item.outcome === "failed" ? false : null,
    duration_seconds: nullableNumber(execution.total_seconds),
    final_text: typeof execution.final_text === "string" ? execution.final_text : "",
    stop_reason: typeof execution.stop_reason === "string" ? execution.stop_reason : "",
    judge: Object.keys(judge).length ? judge : null,
    events: Array.isArray(execution.events) ? execution.events.map(object) : [],
    evidence: { ...metadata, judge_evidence: object(assessment.evidence), error_stage: failure.stage, error_code: failure.code },
    error: typeof failure.message === "string" ? failure.message : "",

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

  get: async (evaluationId: string, signal?: AbortSignal) =>
    normalizeEvaluation(
      await requestJson<unknown>(
        `/api/evals/evaluations/${encodeURIComponent(evaluationId)}?include_bodies=false`,
        { signal },
      ),
    ),

  listPage: async (filters: { since?: string; until?: string; offset: number; limit: number; bot_ids: string[] }, signal?: AbortSignal) => {
    const params = new URLSearchParams({ offset: String(filters.offset), limit: String(filters.limit) });
    if (filters.since) params.set("since", filters.since);
    if (filters.until) params.set("until", filters.until);
    for (const bot of filters.bot_ids) params.append("bot_ids", bot);
    return evaluationList(await requestJson<unknown>(`/api/evals/evaluations?${params}`, { signal }));
  },

  caseDetail: async (evaluationId: string, caseRef: string, selection: { trial_id?: string; target_id?: string; attempt?: string } = {}, signal?: AbortSignal) => {
    const response = await requestJson<{
      case_ref?: string;
      comparison?: Record<string, unknown> | null;
      trials?: unknown[];
    }>(
      `/api/evals/evaluations/${encodeURIComponent(evaluationId)}/cases/${encodeURIComponent(caseRef)}${queryString(selection)}`,
      { signal },
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
