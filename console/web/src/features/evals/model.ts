export type EvaluationKind = "comparison" | "suite";

export type EvaluationStatus =
  | "queued"
  | "running"
  | "completed"
  | "partial"
  | "cancelled"
  | "interrupted"
  | "error";

export type ComparisonPreset = "quick" | "standard" | "custom";

export type EvaluationOutcome = "passed" | "failed" | "skipped" | "error";

export interface ApiCheck {
  code: string;
  label: string;
  ok: boolean;
  detail: string;
  action: string;
}

export interface ApiProblem {
  code: string;
  message: string;
  checks: ApiCheck[];
}

export interface ComparisonEvaluationRequest {
  kind: "comparison";
  bot_id: string;
  profile_id: string;
  preset: ComparisonPreset;
  target_ids?: string[];
  case_refs?: string[];
  repetitions?: number;
  max_wall_seconds?: number;
  seed?: number;
}

export interface SuiteEvaluationRequest {
  kind: "suite";
  bot_id: string;
  suite_id: string;
  case_ids: string[];
  dry_run: boolean;
  llm_judge: boolean;
}

export type EvaluationRequest =
  | ComparisonEvaluationRequest
  | SuiteEvaluationRequest;

export interface ComparisonFormValues {
  botId: string;
  profileId: string;
  preset: ComparisonPreset;
  targetIds: string[];
  caseRefs: string[];
  repetitions: number;
  maxWallSeconds: number;
  seed: number;
}

export interface SuiteFormValues {
  botId: string;
  suiteId: string;
  caseIds: string[];
  dryRun: boolean;
  llmJudge: boolean;
}

export interface RequestGeneration {
  begin: () => number;
  invalidate: () => void;
  isCurrent: (generation: number) => boolean;
}

export function createRequestGeneration(): RequestGeneration {
  let current = 0;
  return {
    begin: () => {
      current += 1;
      return current;
    },
    invalidate: () => {
      current += 1;
    },
    isCurrent: (generation) => generation === current,
  };
}

export interface SelectionSnapshot {
  id: string;
  generation: number;
}

export function isCurrentSelection(
  current: SelectionSnapshot,
  captured: SelectionSnapshot,
): boolean {
  return current.id === captured.id &&
    current.generation === captured.generation;
}

function canonicalJsonValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonicalJsonValue);
  if (typeof value !== "object" || value === null) return value;
  return Object.fromEntries(
    Object.entries(value)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([key, item]) => [key, canonicalJsonValue(item)]),
  );
}

export function evaluationEventKey(
  event: Record<string, unknown>,
): string {
  return JSON.stringify(canonicalJsonValue(event));
}

export function acceptUniqueEvaluationEvent(
  seen: Set<string>,
  event: Record<string, unknown>,
): boolean {
  const key = evaluationEventKey(event);
  if (seen.has(key)) return false;
  seen.add(key);
  return true;
}

export function shouldStopEvaluationStream(
  consecutiveErrors: number,
  readyState: number,
): boolean {
  return readyState === 2 || consecutiveErrors >= 3;
}

export function retainAvailableSelection(
  current: string,
  available: readonly string[],
): string {
  return current && available.includes(current) ? current : "";
}

export interface EvaluationTarget {
  target_id: string;
  label: string;
  executor: string;
  backend: string;
  model: string;
  reasoning_effort: string;
  fingerprint: string;
}

export interface EvaluationProgress {
  completed: number;
  total: number;
  percent: number;
  current: string;
}

export interface EvaluationRecord {
  evaluation_id: string;
  kind: EvaluationKind;
  bot_id: string;
  status: EvaluationStatus;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  duration_seconds: number | null;
  progress: EvaluationProgress;
  targets: EvaluationTarget[];
  selection: Record<string, unknown>;
  request: Record<string, unknown>;
  result: Record<string, unknown> | null;
  error: string;
}

export interface ProfileCase {
  ref: string;
  suite_id: string;
  case_id: string;
  dimension: string;
  category: string;
  summary: string;
  source: string;
}

export interface EvaluationProfile {
  profile_id: string;
  name: string;
  description: string;
  default_seed: number;
  modes: Record<string, { repetitions: number; max_wall_seconds: number }>;
  dimensions: string[];
  cases: ProfileCase[];
}

export interface EvaluationParameter {
  name: "dry_run" | "llm_judge" | string;
  type: "boolean";
  label: string;
  default: boolean;
}

export interface EvaluationSuite {
  suite_id: string;
  name: string;
  kind: string;
  value: string;
  recommendation: string;
  cadence: string;
  requires_bot: boolean;
  requires_external_data: boolean;
  official_url: string;
  setup_hint: string;
  implemented: boolean;
  ready: boolean;
  case_count: number;
  unavailable_reason: string;
  prepare_available: boolean;
  parameters: EvaluationParameter[];
  selection_policy?: string;
  level_policy?: string;
  category_policy?: string;
  data_source?: string;
  data_cache_path?: string;
  uses_smoke_data?: boolean;
}

export function suiteSupportsLlmJudge(suite: EvaluationSuite | null): boolean {
  return Boolean(
    suite &&
      (suite.suite_id === "gaia" ||
        suite.parameters.some((parameter) => parameter.name === "llm_judge")),
  );
}

export interface EvaluationCaseSummary {
  case_id: string;
  category: string;
  summary: string;
  has_attachments: boolean;
  attachment_count: number;
  source: string;
}

export interface EvaluationCaseDescriptor extends EvaluationCaseSummary {
  input: string;
  context: string;
  rubric: string;
  expected_behavior: string;
  metadata: Record<string, unknown>;
}

export interface EvaluationTrial {
  trial_id: string;
  case_ref: string;
  case_id: string;
  dimension: string;
  target_id: string;
  target_fingerprint: string;
  attempt: number;
  outcome: EvaluationOutcome | string;
  score: number | null;
  max_score: number | null;
  passed: boolean | null;
  duration_seconds: number | null;
  final_text: string;
  stop_reason: string;
  judge: Record<string, unknown> | null;
  events: Array<Record<string, unknown>>;
  evidence: Record<string, unknown>;
  error: string;
}

export interface EvaluationCaseDetail {
  case_ref: string;
  comparison?: Record<string, unknown> | null;
  trials: EvaluationTrial[];
}

export interface CoverageHistory {
  trial_id: string;
  evaluation_id: string;
  attempt: number;
  outcome: string;
  score: number | null;
  duration_seconds: number | null;
  finished_at: string;
}

export interface EvaluationCoverageRecord {
  bot_id: string;
  suite_id: string;
  case_ref: string;
  case_id: string;
  target_id: string;
  target_fingerprint: string;
  completed_count: number;
  last_outcome: string;
  last_score: number | null;
  last_completed_at: string;
  history: CoverageHistory[];
}

export interface EvaluationCoverage {
  generated_at: string;
  summary: {
    case_count: number;
    failed_case_count: number;
    bot_count: number;
    target_count: number;
  };
  records: EvaluationCoverageRecord[];
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function asRecord(value: unknown): Record<string, unknown> {
  return isRecord(value) ? value : {};
}

function asString(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function asNumber(value: unknown, fallback = 0): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function asNullableNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function normalizeCheck(value: unknown, index: number): ApiCheck {
  const item = asRecord(value);
  return {
    code: asString(item.code, `check_${index + 1}`),
    label: asString(item.label, asString(item.code, `检查 ${index + 1}`)),
    ok: item.ok === true,
    detail: asString(item.detail, asString(item.message)),
    action: asString(item.action),
  };
}

function formatValidationItem(value: unknown): string {
  if (!isRecord(value)) return formatUnknown(value);
  const location = Array.isArray(value.loc)
    ? value.loc
        .filter((part) => part !== "body")
        .map((part) => String(part))
        .join(".")
    : "";
  const message = asString(value.msg, asString(value.message, "请求参数无效"));
  return location ? `${location}：${message}` : message;
}

function formatUnknown(value: unknown): string {
  if (typeof value === "string") return value.trim();
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  if (value == null) return "";
  if (Array.isArray(value)) {
    return value.map(formatValidationItem).filter(Boolean).join("；");
  }
  if (isRecord(value)) {
    const message = asString(value.message, asString(value.error));
    if (message) return message;
    try {
      return JSON.stringify(value);
    } catch {
      return "请求失败";
    }
  }
  return "请求失败";
}

export function parseApiProblem(
  payload: unknown,
  fallback = "请求失败",
): ApiProblem {
  const envelope = asRecord(payload);
  const detail = "detail" in envelope ? envelope.detail : payload;

  if (Array.isArray(detail)) {
    return {
      code: "validation_error",
      message: detail.map(formatValidationItem).filter(Boolean).join("；") || fallback,
      checks: [],
    };
  }

  if (isRecord(detail)) {
    const checks = Array.isArray(detail.checks)
      ? detail.checks.map(normalizeCheck)
      : [];
    const message = asString(
      detail.message,
      asString(detail.error, checks.find((check) => !check.ok)?.detail || fallback),
    );
    return {
      code: asString(detail.code, "request_failed"),
      message,
      checks,
    };
  }

  return {
    code: "request_failed",
    message: formatUnknown(detail) || fallback,
    checks: [],
  };
}

export function formatApiError(error: unknown): string {
  if (error instanceof EvaluationApiError) return error.problem.message;
  if (error instanceof Error) return error.message;
  return parseApiProblem(error).message;
}

export class EvaluationApiError extends Error {
  readonly status: number;
  readonly problem: ApiProblem;

  constructor(status: number, problem: ApiProblem) {
    super(problem.message);
    this.name = "EvaluationApiError";
    this.status = status;
    this.problem = problem;
  }
}

export function buildComparisonRequest(
  values: ComparisonFormValues,
): ComparisonEvaluationRequest {
  const base: ComparisonEvaluationRequest = {
    kind: "comparison",
    bot_id: values.botId,
    profile_id: values.profileId,
    preset: values.preset,
  };
  if (values.preset !== "custom") return base;
  return {
    ...base,
    target_ids: [...values.targetIds],
    case_refs: [...values.caseRefs],
    repetitions: values.repetitions,
    max_wall_seconds: values.maxWallSeconds,
    seed: values.seed,
  };
}

export function buildSuiteRequest(values: SuiteFormValues): SuiteEvaluationRequest {
  return {
    kind: "suite",
    bot_id: values.botId,
    suite_id: values.suiteId,
    case_ids: [...values.caseIds],
    dry_run: values.dryRun,
    llm_judge: values.llmJudge,
  };
}

function normalizeTarget(value: unknown, index: number): EvaluationTarget {
  const item = asRecord(value);
  const targetId = asString(item.target_id, `target-${index + 1}`);
  return {
    target_id: targetId,
    label: asString(item.label, targetId),
    executor: asString(item.executor),
    backend: asString(item.backend),
    model: asString(item.model),
    reasoning_effort: asString(item.reasoning_effort),
    fingerprint: asString(item.fingerprint),
  };
}

function inferTotal(
  kind: EvaluationKind,
  request: Record<string, unknown>,
  targets: EvaluationTarget[],
): number {
  if (kind === "suite") {
    return Array.isArray(request.case_ids) ? request.case_ids.length : 0;
  }
  const caseCount = Array.isArray(request.case_refs) ? request.case_refs.length : 0;
  const targetCount = targets.length ||
    (Array.isArray(request.target_ids) ? request.target_ids.length : 0);
  return caseCount * targetCount * asNumber(request.repetitions, 1);
}

export function normalizeEvaluation(value: unknown): EvaluationRecord {
  const item = asRecord(value);
  const request = asRecord(item.request);
  const result =
    isRecord(item.result) && Object.keys(item.result).length > 0
      ? item.result
      : null;
  const kind: EvaluationKind = item.kind === "suite" ? "suite" : "comparison";
  const targetValues = Array.isArray(item.targets) ? item.targets : [];
  const targets = targetValues.map(normalizeTarget);
  const progressValue = asRecord(item.progress);
  const completed = asNumber(
    progressValue.completed,
    asNumber(item.completed_trials),
  );
  const total = asNumber(
    progressValue.total,
    asNumber(item.planned_trials, inferTotal(kind, request, targets)),
  );
  const suppliedPercent = asNullableNumber(progressValue.percent);
  const percent = suppliedPercent == null
    ? total > 0
      ? Math.min(100, Math.round((completed / total) * 100))
      : 0
    : Math.max(0, Math.min(100, suppliedPercent));

  return {
    evaluation_id: asString(item.evaluation_id),
    kind,
    bot_id: asString(item.bot_id),
    status: asString(item.status, "error") as EvaluationStatus,
    created_at: asString(item.created_at),
    started_at: typeof item.started_at === "string" ? item.started_at : null,
    finished_at: typeof item.finished_at === "string" ? item.finished_at : null,
    duration_seconds: asNullableNumber(item.duration_seconds),
    progress: {
      completed,
      total,
      percent,
      current: asString(progressValue.current),
    },
    targets,
    selection: asRecord(item.selection),
    request,
    result,
    error: asString(item.error),
  };
}

export function normalizeCoverage(value: unknown): EvaluationCoverage {
  const envelope = asRecord(value);
  const rawRecords = Array.isArray(envelope.records) ? envelope.records : [];
  const records = rawRecords.map((raw): EvaluationCoverageRecord => {
    const item = asRecord(raw);
    const rawHistory = Array.isArray(item.history) ? item.history : [];
    return {
      bot_id: asString(item.bot_id),
      suite_id: asString(item.suite_id),
      case_ref: asString(item.case_ref),
      case_id: asString(item.case_id),
      target_id: asString(item.target_id),
      target_fingerprint: asString(item.target_fingerprint),
      completed_count: asNumber(item.completed_count),
      last_outcome: asString(item.last_outcome),
      last_score: asNullableNumber(item.last_score),
      last_completed_at: asString(item.last_completed_at),
      history: rawHistory.map((rawItem): CoverageHistory => {
        const history = asRecord(rawItem);
        return {
          trial_id: asString(history.trial_id),
          evaluation_id: asString(history.evaluation_id),
          attempt: asNumber(history.attempt),
          outcome: asString(history.outcome),
          score: asNullableNumber(history.score),
          duration_seconds: asNullableNumber(history.duration_seconds),
          finished_at: asString(history.finished_at),
        };
      }),
    };
  });
  const rawSummary = asRecord(envelope.summary);
  return {
    generated_at: asString(envelope.generated_at),
    summary: {
      case_count: asNumber(rawSummary.case_count, records.length),
      failed_case_count: asNumber(rawSummary.failed_case_count),
      bot_count: asNumber(rawSummary.bot_count),
      target_count: asNumber(
        rawSummary.target_count,
        new Set(records.map((item) => item.target_fingerprint).filter(Boolean)).size,
      ),
    },
    records,
  };
}
