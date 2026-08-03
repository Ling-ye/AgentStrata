import { describe, expect, it } from "vitest";

import {
  acceptUniqueEvaluationEvent,
  buildComparisonRequest,
  buildSuiteRequest,
  createRequestGeneration,
  isCurrentSelection,
  normalizeCoverage,
  normalizeEvaluation,
  parseApiProblem,
  retainAvailableSelection,
  shouldStopEvaluationStream,
  suiteSupportsLlmJudge,
} from "./model";

describe("evaluation request builders", () => {
  const comparison = {
    botId: "lingye-copilot-qq",
    profileId: "agent-comparison-mvp",
    targetIds: ["codex", "native"],
    caseRefs: ["profile/case-a", "profile/case-b"],
    repetitions: 3,
    maxWallSeconds: 2700,
    seed: 20260723,
  };

  it("omits custom-only fields from quick and standard requests", () => {
    expect(buildComparisonRequest({ ...comparison, preset: "quick" })).toEqual({
      kind: "comparison",
      bot_id: "lingye-copilot-qq",
      profile_id: "agent-comparison-mvp",
      preset: "quick",
    });
    expect(buildComparisonRequest({ ...comparison, preset: "standard" })).toEqual({
      kind: "comparison",
      bot_id: "lingye-copilot-qq",
      profile_id: "agent-comparison-mvp",
      preset: "standard",
    });
  });

  it("includes every explicit field only for custom comparison", () => {
    expect(buildComparisonRequest({ ...comparison, preset: "custom" })).toEqual({
      kind: "comparison",
      bot_id: "lingye-copilot-qq",
      profile_id: "agent-comparison-mvp",
      preset: "custom",
      target_ids: ["codex", "native"],
      case_refs: ["profile/case-a", "profile/case-b"],
      repetitions: 3,
      max_wall_seconds: 2700,
      seed: 20260723,
    });
  });

  it("builds the strict suite request shape", () => {
    expect(buildSuiteRequest({
      botId: "sample-bot",
      suiteId: "bfcl",
      caseIds: ["simple_1"],
      dryRun: true,
      llmJudge: false,
    })).toEqual({
      kind: "suite",
      bot_id: "sample-bot",
      suite_id: "bfcl",
      case_ids: ["simple_1"],
      dry_run: true,
      llm_judge: false,
    });
  });

  it("exposes LLM judge only for GAIA or an explicitly declared parameter", () => {
    const suite = {
      suite_id: "bfcl",
      name: "BFCL",
      kind: "function",
      value: "",
      recommendation: "",
      cadence: "",
      requires_bot: true,
      requires_external_data: false,
      official_url: "",
      setup_hint: "",
      implemented: true,
      ready: true,
      case_count: 1,
      unavailable_reason: "",
      prepare_available: false,
      parameters: [],
    };
    expect(suiteSupportsLlmJudge(suite)).toBe(false);
    expect(suiteSupportsLlmJudge({ ...suite, suite_id: "gaia" })).toBe(true);
    expect(suiteSupportsLlmJudge({
      ...suite,
      parameters: [{
        name: "llm_judge",
        type: "boolean",
        label: "Judge",
        default: false,
      }],
    })).toBe(true);
  });
});

describe("API problem formatting", () => {
  it("renders structured blocking checks without object coercion", () => {
    const problem = parseApiProblem({
      detail: {
        code: "preflight_blocked",
        message: "启动检查未通过",
        checks: [
          {
            code: "model",
            label: "模型配置",
            ok: false,
            detail: "缺少 code model",
            action: "配置 llm.code",
          },
        ],
      },
    });
    expect(problem.message).toBe("启动检查未通过");
    expect(problem.checks[0]).toMatchObject({
      code: "model",
      label: "模型配置",
      detail: "缺少 code model",
      ok: false,
    });
    expect(problem.message).not.toContain("[object Object]");
  });

  it("renders every Pydantic validation location and message", () => {
    const problem = parseApiProblem({
      detail: [
        { loc: ["body", "target_ids"], msg: "Extra inputs are not permitted" },
        { loc: ["body", "case_refs", 0], msg: "Field required" },
      ],
    });
    expect(problem.message).toBe(
      "target_ids：Extra inputs are not permitted；case_refs.0：Field required",
    );
  });
});

describe("asynchronous request ownership", () => {
  it("invalidates both success and error callbacks from an older submission", () => {
    const requests = createRequestGeneration();
    const first = requests.begin();
    expect(requests.isCurrent(first)).toBe(true);

    requests.invalidate();
    expect(requests.isCurrent(first)).toBe(false);

    const second = requests.begin();
    expect(requests.isCurrent(first)).toBe(false);
    expect(requests.isCurrent(second)).toBe(true);
  });

  it("lets only the latest Case preview request update state", () => {
    const requests = createRequestGeneration();
    const firstSuite = requests.begin();
    const secondSuite = requests.begin();

    expect(requests.isCurrent(firstSuite)).toBe(false);
    expect(requests.isCurrent(secondSuite)).toBe(true);
  });

  it("invalidates an in-flight Case evidence request when Evaluation changes", () => {
    const requests = createRequestGeneration();
    const firstEvaluation = requests.begin();

    requests.invalidate();

    expect(requests.isCurrent(firstEvaluation)).toBe(false);
    const secondEvaluation = requests.begin();
    expect(requests.isCurrent(secondEvaluation)).toBe(true);
  });

  it("rejects queued events from an older Evaluation selection", () => {
    const captured = { id: "eval_a", generation: 1 };
    expect(isCurrentSelection(captured, captured)).toBe(true);
    expect(isCurrentSelection(
      { id: "eval_b", generation: 2 },
      captured,
    )).toBe(false);
    expect(isCurrentSelection(
      { id: "eval_a", generation: 3 },
      captured,
    )).toBe(false);
  });

  it("rejects a late rerun result after the record selection changes", () => {
    const rerunOwner = { id: "eval_a", generation: 4 };
    expect(isCurrentSelection(
      { id: "eval_b", generation: 5 },
      rerunOwner,
    )).toBe(false);
  });
});

describe("Evaluation event streams", () => {
  it("deduplicates replayed events by stable content rather than object identity", () => {
    const seen = new Set<string>();
    expect(acceptUniqueEvaluationEvent(seen, {
      event: "trial_completed",
      completed_trials: 1,
      nested: { target: "codex", attempt: 1 },
    })).toBe(true);
    expect(acceptUniqueEvaluationEvent(seen, {
      nested: { attempt: 1, target: "codex" },
      completed_trials: 1,
      event: "trial_completed",
    })).toBe(false);
    expect(acceptUniqueEvaluationEvent(seen, {
      event: "trial_completed",
      completed_trials: 2,
    })).toBe(true);
  });

  it("stops a permanently closed stream or repeated connection failures", () => {
    expect(shouldStopEvaluationStream(1, 2)).toBe(true);
    expect(shouldStopEvaluationStream(2, 0)).toBe(false);
    expect(shouldStopEvaluationStream(3, 0)).toBe(true);
  });
});

describe("coverage filters", () => {
  it("keeps an available Target fingerprint and clears a stale one", () => {
    const available = ["sha256:new-target"];
    expect(retainAvailableSelection("sha256:new-target", available)).toBe(
      "sha256:new-target",
    );
    expect(retainAvailableSelection("sha256:old-target", available)).toBe("");
    expect(retainAvailableSelection("sha256:old-target", [])).toBe("");
  });
});

describe("Evaluation record adapter", () => {
  it("normalizes progress and keeps lifecycle separate from result", () => {
    expect(normalizeEvaluation({
      evaluation_id: "eval_123",
      kind: "comparison",
      bot_id: "lingye-copilot-qq",
      status: "completed",
      created_at: "2026-07-26T00:00:00Z",
      progress: { completed: 4, total: 8, percent: 50 },
      targets: [{ target_id: "codex", fingerprint: "sha256:codex" }],
      request: { kind: "comparison", repetitions: 1 },
      result: {
        trials: [{ trial_id: "trial_1", outcome: "failed" }],
        summary: { outcomes: { failed: 1 } },
      },
    })).toMatchObject({
      evaluation_id: "eval_123",
      kind: "comparison",
      status: "completed",
      progress: { completed: 4, total: 8, percent: 50 },
      result: {
        trials: [{ trial_id: "trial_1", outcome: "failed" }],
        summary: { outcomes: { failed: 1 } },
      },
    });
  });

  it("keeps coverage outcome distinct from Evaluation lifecycle", () => {
    expect(normalizeCoverage({
      summary: {
        case_count: 1,
        failed_case_count: 1,
        bot_count: 1,
        target_count: 1,
      },
      records: [{
        bot_id: "lingye-copilot-qq",
        suite_id: "bfcl",
        case_id: "simple_1",
        target_id: "chat-direct",
        target_fingerprint: "sha256:target",
        completed_count: 1,
        last_outcome: "failed",
        history: [{
          trial_id: "trial_1",
          evaluation_id: "eval_123",
          attempt: 1,
          outcome: "failed",
          finished_at: "2026-07-26T00:00:00Z",
        }],
      }],
    }).records[0]).toMatchObject({
      last_outcome: "failed",
      history: [{ trial_id: "trial_1", attempt: 1, outcome: "failed" }],
    });
  });

  it("treats a missing persisted result as no result", () => {
    expect(normalizeEvaluation({
      evaluation_id: "eval_error",
      kind: "suite",
      bot_id: "sample-bot",
      status: "error",
      created_at: "2026-07-26T00:00:00Z",
      progress: { completed: 0, total: 1, percent: 0 },
      result: {},
    }).result).toBeNull();
  });
});
