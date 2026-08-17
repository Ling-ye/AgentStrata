import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Collapse,
  Descriptions,
  Drawer,
  Empty,
  Input,
  InputNumber,
  Message,
  Modal,
  Progress,
  Radio,
  Select,
  Space,
  Spin,
  Table,
  Tabs,
  Tag,
  Typography,
} from "@arco-design/web-react";

import { api, streamTask } from "../api";
import type { BotInstance } from "../types";
import {
  evaluationApi,
  evaluationExportUrl,
  streamEvaluation,
} from "../features/evals/evaluationApi";
import {
  acceptUniqueEvaluationEvent,
  buildComparisonRequest,
  buildSuiteOptions,
  buildSuiteRequest,
  createRequestGeneration,
  EvaluationApiError,
  formatApiError,
  isCurrentSelection,
  isProductCapabilityEvaluation,
  productCapabilityResultView,
  retainAvailableSelection,
  suitePresetRequiresExternalWrite,
  suiteSupportsLlmJudge,
  type ApiProblem,
  type ComparisonPreset,
  type EvaluationCaseDescriptor,
  type EvaluationCaseDetail,
  type EvaluationCaseSummary,
  type EvaluationCoverage,
  type EvaluationCoverageRecord,
  type EvaluationKind,
  type EvaluationProfile,
  type ProductCapabilityGroup,
  type EvaluationRecord,
  type EvaluationRequest,
  type EvaluationStatus,
  type EvaluationSuite,
  type SelectionSnapshot,
  type SuitePreset,
} from "../features/evals/model";
import { useEventStreamLines } from "../shared/hooks/useEventStreamLines";
import type { ColumnProps } from "../shared/ui/arcoTypes";
import PageSection from "../shared/ui/PageSection";
import TaskStreamSheet from "../shared/ui/TaskStreamSheet";

const { Paragraph, Text, Title } = Typography;

const ACTIVE_STATUSES = new Set<EvaluationStatus>(["queued", "running"]);
const STATUS_COLORS: Record<string, string> = {
  queued: "gray",
  running: "arcoblue",
  completed: "green",
  partial: "orange",
  cancelled: "gray",
  interrupted: "orangered",
  error: "red",
  passed: "green",
  failed: "red",
  skipped: "gray",
  tie: "gray",
  inconclusive: "orange",
  codex: "purple",
  native: "cyan",
  "error/indeterminate": "red",
  in_progress: "arcoblue",
};

const DIMENSION_LABELS: Record<string, string> = {
  instruction_following: "指令遵循",
  knowledge_research: "知识 / 检索",
  tool_orchestration: "工具编排",
  code: "代码任务",
};

const CAPABILITY_LABELS: Record<string, string> = {
  dialogue_constraints: "对话与任务约束",
  tool_orchestration: "工具编排",
  search: "搜索与证据",
  file_workspace: "文件与 Workspace",
  image_understanding: "图片理解",
  session_memory_subagent: "会话、记忆与 Subagent",
  code_recovery: "代码与恢复",
  access_security: "白名单、角色与注入",
  qq_live: "真实 QQ 正向链路",
};

interface EvaluationForm {
  botId: string;
  kind: EvaluationKind;
  profileId: string;
  preset: ComparisonPreset;
  targetIds: string[];
  caseRefs: string[];
  repetitions: number;
  maxWallSeconds: number;
  seed: number;
  suiteId: string;
  caseIds: string[];
  suitePreset: SuitePreset;
  suiteRepetitions: number;
  suiteMaxWallSeconds: number;
  suiteSeed: number;
  confirmExternalWrite: boolean;
  dryRun: boolean;
  llmJudge: boolean;
}

interface Props {
  visible?: boolean;
}

interface StartSubmission {
  generation: number;
  request: EvaluationRequest;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function recordValue(value: unknown): Record<string, unknown> {
  return isRecord(value) ? value : {};
}

function recordArray(value: unknown): Array<Record<string, unknown>> {
  return Array.isArray(value) ? value.filter(isRecord) : [];
}

function stringValue(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function numberValue(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function formatTime(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function formatDuration(value: number | null | undefined): string {
  if (value == null) return "—";
  if (value < 60) return `${value.toFixed(1)}s`;
  return `${Math.floor(value / 60)}m ${Math.round(value % 60)}s`;
}

function formatScore(value: unknown, max?: unknown): string {
  const score = numberValue(value);
  const maximum = numberValue(max);
  if (score == null) return "—";
  return maximum == null ? score.toFixed(2) : `${score.toFixed(2)} / ${maximum.toFixed(2)}`;
}

function formatFingerprint(value: string): string {
  if (!value) return "—";
  return value.length > 20 ? `${value.slice(0, 12)}…${value.slice(-6)}` : value;
}

function coloredTag(value: string) {
  return <Tag color={STATUS_COLORS[value] ?? "gray"} title={value}>{value}</Tag>;
}

function lifecycleTag(status: string) {
  return coloredTag(status);
}

function outcomeTag(outcome: string) {
  return coloredTag(outcome);
}

function verdictTag(verdict: string) {
  return coloredTag(verdict);
}

function kindTag(kind: EvaluationKind) {
  return (
    <Tag color={kind === "comparison" ? "purple" : "arcoblue"}>
      {kind === "comparison" ? "Agent 对比" : "能力 / Suite"}
    </Tag>
  );
}

function evaluationLabel(record: EvaluationRecord): string {
  if (record.kind === "comparison") {
    return stringValue(
      record.request.profile_id,
      stringValue(record.selection.id, "Comparison"),
    );
  }
  return stringValue(
    record.request.suite_id,
    stringValue(record.selection.id, "Suite"),
  );
}

function presetDescription(
  profile: EvaluationProfile | null,
  preset: ComparisonPreset,
): string {
  if (preset === "custom") return "自选 Target、Case、重复次数、预算与 seed";
  const config = profile?.modes[preset];
  if (!config) return preset === "quick" ? "服务端快速策略" : "服务端标准策略";
  return `${config.repetitions} 次重复 · 最长 ${formatDuration(config.max_wall_seconds)}`;
}

export default function EvalsPage({ visible = true }: Props) {
  const queryClient = useQueryClient();
  const prepareStream = useEventStreamLines();
  const [tab, setTab] = useState("create");
  const [form, setForm] = useState<EvaluationForm>({
    botId: "",
    kind: "comparison",
    profileId: "",
    preset: "quick",
    targetIds: ["codex", "native"],
    caseRefs: [],
    repetitions: 1,
    maxWallSeconds: 2700,
    seed: 20260723,
    suiteId: "",
    caseIds: [],
    suitePreset: "custom",
    suiteRepetitions: 1,
    suiteMaxWallSeconds: 0,
    suiteSeed: 0,
    confirmExternalWrite: false,
    dryRun: false,
    llmJudge: false,
  });
  const [startProblem, setStartProblem] = useState<ApiProblem | null>(null);
  const [selectedEvaluationId, setSelectedEvaluationId] = useState("");
  const selectionGeneration = useRef(createRequestGeneration());
  const selectedEvaluation = useRef<SelectionSnapshot>({
    id: "",
    generation: 0,
  });
  const [events, setEvents] = useState<Array<Record<string, unknown>>>([]);
  const eventKeys = useRef(new Set<string>());
  const [caseDetail, setCaseDetail] = useState<EvaluationCaseDetail | null>(null);
  const [caseDetailLoading, setCaseDetailLoading] = useState(false);
  const caseDetailRequest = useRef(createRequestGeneration());
  const [casePreview, setCasePreview] = useState<EvaluationCaseDescriptor | null>(null);
  const casePreviewRequest = useRef(createRequestGeneration());
  const startRequest = useRef(createRequestGeneration());
  const [coverageDetail, setCoverageDetail] = useState<EvaluationCoverageRecord | null>(null);
  const [catalogSuiteId, setCatalogSuiteId] = useState("");
  const [recordKind, setRecordKind] = useState("");
  const [recordBot, setRecordBot] = useState("");
  const [recordStatus, setRecordStatus] = useState("");

  const selectEvaluation = (evaluationId: string) => {
    selectedEvaluation.current = {
      id: evaluationId,
      generation: selectionGeneration.current.begin(),
    };
    caseDetailRequest.current.invalidate();
    setCaseDetailLoading(false);
    setCaseDetail(null);
    eventKeys.current.clear();
    setEvents([]);
    setSelectedEvaluationId(evaluationId);
  };

  const updateForm = (patch: Partial<EvaluationForm>) => {
    startRequest.current.invalidate();
    if ("botId" in patch || "kind" in patch || "suiteId" in patch) {
      casePreviewRequest.current.invalidate();
      setCasePreview(null);
    }
    if ("botId" in patch) {
      setCatalogSuiteId("");
      setCoverageDetail(null);
    }
    setForm((current) => ({ ...current, ...patch }));
    setStartProblem(null);
  };

  const botsQuery = useQuery({
    queryKey: ["bots"],
    queryFn: api.listBots,
    enabled: visible,
  });
  const profilesQuery = useQuery({
    queryKey: ["evaluation-profiles"],
    queryFn: evaluationApi.profiles,
    enabled: visible,
  });
  const suitesQuery = useQuery({
    queryKey: ["evaluation-suites", form.botId],
    queryFn: () => evaluationApi.suites(form.botId),
    enabled: visible && Boolean(form.botId),
  });
  const suiteCasesQuery = useQuery({
    queryKey: ["evaluation-suite-cases", form.botId, form.suiteId],
    queryFn: () => evaluationApi.cases(form.suiteId, form.botId),
    enabled:
      visible &&
      tab === "create" &&
      form.kind === "suite" &&
      Boolean(form.botId && form.suiteId) &&
      Boolean(
        suitesQuery.data?.some(
          (suite) => suite.suite_id === form.suiteId && suite.ready,
        ),
      ),
  });
  const recordsQuery = useQuery({
    queryKey: ["evaluations"],
    queryFn: () => evaluationApi.list(),
    enabled: visible,
    refetchInterval: (query) => {
      const records = query.state.data as EvaluationRecord[] | undefined;
      return visible &&
        (tab === "records" || tab === "create") &&
        records?.some(
          (item) =>
            ACTIVE_STATUSES.has(item.status) &&
            (tab === "records" || item.bot_id === form.botId),
        )
        ? 2000
        : false;
    },
  });
  const detailQuery = useQuery({
    queryKey: ["evaluation", selectedEvaluationId],
    queryFn: () => evaluationApi.get(selectedEvaluationId),
    enabled:
      visible &&
      tab === "records" &&
      Boolean(selectedEvaluationId),
    refetchInterval: (query) => {
      const record = query.state.data as EvaluationRecord | undefined;
      return visible &&
        tab === "records" &&
        record &&
        ACTIVE_STATUSES.has(record.status)
        ? 1500
        : false;
    },
  });
  const coverageQuery = useQuery({
    queryKey: ["evaluation-coverage", form.botId],
    queryFn: () => evaluationApi.coverage(form.botId),
    enabled: visible && tab === "catalog" && Boolean(form.botId),
  });
  const catalogCasesQuery = useQuery({
    queryKey: ["evaluation-suite-cases", form.botId, catalogSuiteId],
    queryFn: () => evaluationApi.cases(catalogSuiteId, form.botId),
    enabled:
      visible &&
      tab === "catalog" &&
      Boolean(form.botId && catalogSuiteId) &&
      Boolean(
        suitesQuery.data?.some(
          (suite) => suite.suite_id === catalogSuiteId && suite.ready,
        ),
      ),
  });

  const bots = botsQuery.data ?? [];
  const profiles = profilesQuery.data ?? [];
  const suites = suitesQuery.data ?? [];
  const records = recordsQuery.data ?? [];
  const selectedProfile =
    profiles.find((item) => item.profile_id === form.profileId) ?? null;
  const selectedSuite =
    suites.find((item) => item.suite_id === form.suiteId) ?? null;
  const detail = detailQuery.data ?? null;
  const activeForBot = records.find(
    (item) => item.bot_id === form.botId && ACTIVE_STATUSES.has(item.status),
  );

  useEffect(() => {
    if (!form.botId && bots.length) {
      setForm((current) => ({ ...current, botId: bots[0].instance_id }));
    }
  }, [bots, form.botId]);

  useEffect(() => {
    if (!profiles.length) return;
    if (profiles.some((profile) => profile.profile_id === form.profileId)) return;
    const profile = profiles[0];
    startRequest.current.invalidate();
    setForm((current) => ({
      ...current,
      profileId: profile.profile_id,
      caseRefs: profile.cases.map((item) => item.ref),
      seed: profile.default_seed,
    }));
  }, [form.profileId, profiles]);

  useEffect(() => {
    if (!suites.length) {
      if (form.suiteId) {
        startRequest.current.invalidate();
        casePreviewRequest.current.invalidate();
        setCasePreview(null);
        setForm((current) => ({ ...current, suiteId: "", caseIds: [] }));
      }
      return;
    }
    if (suites.some((suite) => suite.suite_id === form.suiteId)) return;
    startRequest.current.invalidate();
    casePreviewRequest.current.invalidate();
    setCasePreview(null);
    setForm((current) => ({
      ...current,
      suiteId: suites[0].suite_id,
      caseIds: [],
    }));
  }, [form.suiteId, suites]);

  useEffect(() => {
    if (!selectedProfile) return;
    startRequest.current.invalidate();
    setForm((current) => ({
      ...current,
      caseRefs: selectedProfile.cases.map((item) => item.ref),
      seed: selectedProfile.default_seed,
    }));
  }, [selectedProfile?.profile_id]);

  useEffect(() => {
    if (!selectedSuite) return;
    startRequest.current.invalidate();
    const defaults = new Map(
      selectedSuite.parameters.map((item) => [item.name, item.default]),
    );
    const declaredPresets = (selectedSuite.presets ?? [])
      .map((item) => item.preset_id)
      .filter((value): value is SuitePreset =>
        ["quick", "full", "security", "qq-live", "custom"].includes(value),
      );
    const preferredPreset = selectedSuite.default_preset;
    const suitePreset = declaredPresets.includes(preferredPreset as SuitePreset)
      ? preferredPreset as SuitePreset
      : declaredPresets[0] ?? "custom";
    setForm((current) => ({
      ...current,
      caseIds: [],
      suitePreset,
      suiteRepetitions: 1,
      suiteMaxWallSeconds: 0,
      suiteSeed: 0,
      confirmExternalWrite: false,
      dryRun: defaults.get("dry_run") ?? false,
      llmJudge: suiteSupportsLlmJudge(selectedSuite)
        ? defaults.get("llm_judge") ?? false
        : false,
    }));
  }, [selectedSuite?.suite_id]);

  useEffect(() => {
    if (
      !visible ||
      tab !== "records" ||
      !detail ||
      !ACTIVE_STATUSES.has(detail.status)
    ) {
      return;
    }
    const evaluationId = detail.evaluation_id;
    const capturedSelection = { ...selectedEvaluation.current };
    if (capturedSelection.id !== evaluationId) return;
    return streamEvaluation(
      evaluationId,
      (event) => {
        if (!isCurrentSelection(selectedEvaluation.current, capturedSelection)) {
          return;
        }
        if (!acceptUniqueEvaluationEvent(eventKeys.current, event)) return;
        setEvents((current) => [...current.slice(-199), event]);
      },
      () => {
        void queryClient.invalidateQueries({
          queryKey: ["evaluation", evaluationId],
        });
        void queryClient.invalidateQueries({ queryKey: ["evaluations"] });
      },
    );
  }, [
    detail?.evaluation_id,
    detail?.status,
    queryClient,
    tab,
    visible,
  ]);

  const startMutation = useMutation({
    mutationFn: (submission: StartSubmission) =>
      evaluationApi.create(submission.request),
    onSuccess: (created, submission) => {
      void queryClient.invalidateQueries({ queryKey: ["evaluations"] });
      if (!startRequest.current.isCurrent(submission.generation)) return;
      setStartProblem(null);
      selectEvaluation(created.evaluation_id);
      setTab("records");
      Message.success(`评测已启动：${created.evaluation_id}`);
    },
    onError: (error, submission) => {
      if (!startRequest.current.isCurrent(submission.generation)) return;
      setStartProblem(
        error instanceof EvaluationApiError
          ? error.problem
          : {
              code: "client_validation",
              message: formatApiError(error),
              checks: [],
            },
      );
    },
  });

  const startEvaluation = () => {
    const generation = startRequest.current.begin();
    setStartProblem(null);
    try {
      if (!recordsQuery.isSuccess) {
        throw new Error("无法确认该 Bot 的活动评测状态，已禁止启动");
      }
      if (!form.botId) throw new Error("请选择 Bot");
      if (activeForBot) {
        throw new Error(`该 Bot 已有活动评测：${activeForBot.evaluation_id}`);
      }

      let request: EvaluationRequest;
      if (form.kind === "comparison") {
        if (!form.profileId) throw new Error("请选择 Profile");
        if (form.preset === "custom" && !form.targetIds.length) {
          throw new Error("Custom 至少选择一个 Target");
        }
        if (form.preset === "custom" && !form.caseRefs.length) {
          throw new Error("Custom 至少选择一个 Case");
        }
        request = buildComparisonRequest({
          botId: form.botId,
          profileId: form.profileId,
          preset: form.preset,
          targetIds: form.targetIds,
          caseRefs: form.caseRefs,
          repetitions: form.repetitions,
          maxWallSeconds: form.maxWallSeconds,
          seed: form.seed,
        });
      } else {
        if (!selectedSuite?.ready) throw new Error("请选择已就绪的 Suite");
        if (form.suitePreset === "custom" && !form.caseIds.length) {
          throw new Error("Custom 至少选择一个 Case");
        }
        if (
          suitePresetRequiresExternalWrite(
            selectedSuite,
            form.suitePreset,
            form.caseIds,
          ) &&
          !form.confirmExternalWrite
        ) {
          throw new Error("该 Preset 会向真实 QQ 发送消息，必须确认本次外部写入");
        }
        request = buildSuiteRequest({
          botId: form.botId,
          suiteId: form.suiteId,
          caseIds: form.suitePreset === "custom" ? form.caseIds : [],
          preset: form.suitePreset,
          repetitions: form.suiteRepetitions,
          maxWallSeconds: form.suiteMaxWallSeconds,
          seed: form.suiteSeed,
          options: buildSuiteOptions(selectedSuite, {
            dryRun: form.dryRun,
            llmJudge: form.llmJudge,
          }),
          confirmExternalWrite: form.confirmExternalWrite,
          dryRun: form.dryRun,
          llmJudge: suiteSupportsLlmJudge(selectedSuite) && form.llmJudge,
        });
      }
      // Consume external-write approval before dispatch.  This is intentionally
      // independent of response-generation ownership: a stale success/error
      // callback must not leave one approval armed for a later Evaluation.
      if (
        "confirm_external_write" in request &&
        request.confirm_external_write
      ) {
        setForm((current) => ({ ...current, confirmExternalWrite: false }));
      }
      startMutation.mutate({ generation, request });
    } catch (error) {
      if (!startRequest.current.isCurrent(generation)) return;
      setStartProblem({
        code: "client_validation",
        message: formatApiError(error),
        checks: [],
      });
    }
  };

  const filteredRecords = useMemo(
    () =>
      records.filter((item) => {
        if (recordKind && item.kind !== recordKind) return false;
        if (recordBot && item.bot_id !== recordBot) return false;
        if (recordStatus && item.status !== recordStatus) return false;
        return true;
      }),
    [recordBot, recordKind, recordStatus, records],
  );
  const contextualQueryError =
    botsQuery.error ||
    (tab === "create"
      ? profilesQuery.error ||
        suitesQuery.error ||
        (form.kind === "suite" ? suiteCasesQuery.error : null)
      : null) ||
    (tab === "catalog"
      ? profilesQuery.error ||
        suitesQuery.error ||
        coverageQuery.error ||
        catalogCasesQuery.error
      : null);

  const prepareSuite = async (suite: EvaluationSuite) => {
    try {
      const task = await evaluationApi.prepareSuite(suite.suite_id, form.botId);
      prepareStream.start(
        (onLine, _onStatus, onEnd) =>
          streamTask(task.id, onLine, () => {
            onEnd();
            void queryClient.invalidateQueries({
              queryKey: ["evaluation-suites", form.botId],
            });
            void queryClient.invalidateQueries({
              queryKey: ["evaluation-suite-cases", form.botId, suite.suite_id],
            });
          }),
        { title: `准备 ${suite.name} 数据`, running: true },
      );
    } catch (error) {
      Message.error(formatApiError(error));
    }
  };

  const openCasePreview = async (
    suiteId: string,
    item: EvaluationCaseSummary,
  ) => {
    const generation = casePreviewRequest.current.begin();
    const botId = form.botId;
    try {
      const next = await evaluationApi.caseDescriptor(suiteId, item.case_id, botId);
      if (casePreviewRequest.current.isCurrent(generation)) setCasePreview(next);
    } catch (error) {
      if (casePreviewRequest.current.isCurrent(generation)) {
        Message.error(formatApiError(error));
      }
    }
  };

  const closeCasePreview = () => {
    casePreviewRequest.current.invalidate();
    setCasePreview(null);
  };

  const openEvaluationCase = async (caseRef: string) => {
    if (!detail || detail.evaluation_id !== selectedEvaluationId) return;
    const generation = caseDetailRequest.current.begin();
    const evaluationId = selectedEvaluationId;
    setCaseDetailLoading(true);
    try {
      const next = await evaluationApi.caseDetail(evaluationId, caseRef);
      if (caseDetailRequest.current.isCurrent(generation)) setCaseDetail(next);
    } catch (error) {
      if (caseDetailRequest.current.isCurrent(generation)) {
        Message.error(formatApiError(error));
      }
    } finally {
      if (caseDetailRequest.current.isCurrent(generation)) {
        setCaseDetailLoading(false);
      }
    }
  };

  const closeCaseDetail = () => {
    caseDetailRequest.current.invalidate();
    setCaseDetailLoading(false);
    setCaseDetail(null);
  };

  const cancelRecord = (record: EvaluationRecord) => {
    Modal.confirm({
      title: "取消评测",
      content: `停止 ${record.evaluation_id}？已完成的 Target 组 checkpoint 会保留。`,
      onOk: async () => {
        try {
          await evaluationApi.cancel(record.evaluation_id);
          await queryClient.invalidateQueries({ queryKey: ["evaluations"] });
          await queryClient.invalidateQueries({
            queryKey: ["evaluation", record.evaluation_id],
          });
        } catch (error) {
          Message.error(formatApiError(error));
        }
      },
    });
  };

  const rerunRecord = async (record: EvaluationRecord) => {
    const capturedSelection = { ...selectedEvaluation.current };
    try {
      const created = await evaluationApi.rerun(record.evaluation_id);
      await queryClient.invalidateQueries({ queryKey: ["evaluations"] });
      if (isCurrentSelection(selectedEvaluation.current, capturedSelection)) {
        selectEvaluation(created.evaluation_id);
      }
    } catch (error) {
      if (isCurrentSelection(selectedEvaluation.current, capturedSelection)) {
        Message.error(formatApiError(error));
      }
    }
  };

  const deleteRecord = (record: EvaluationRecord) => {
    Modal.confirm({
      title: "删除评测记录",
      content: `永久删除 ${record.evaluation_id} 的脱敏报告和证据？`,
      okButtonProps: { status: "danger" },
      onOk: async () => {
        try {
          await evaluationApi.remove(record.evaluation_id);
          if (selectedEvaluation.current.id === record.evaluation_id) {
            selectEvaluation("");
          }
          await queryClient.invalidateQueries({ queryKey: ["evaluations"] });
        } catch (error) {
          Message.error(formatApiError(error));
        }
      },
    });
  };

  const loading = botsQuery.isLoading || profilesQuery.isLoading;
  return (
    <PageSection
      title="测评中心"
      description="从一处创建 Agent 对比或基准评测，并用统一的生命周期、记录和证据查看结果。"
      extra={
        <Button
          size="small"
          onClick={() => {
            void Promise.all([
              queryClient.invalidateQueries({ queryKey: ["bots"] }),
              queryClient.invalidateQueries({
                queryKey: ["evaluation-profiles"],
              }),
              queryClient.invalidateQueries({
                queryKey: ["evaluation-suites"],
              }),
              queryClient.invalidateQueries({
                queryKey: ["evaluation-suite-cases"],
              }),
              queryClient.invalidateQueries({
                queryKey: ["evaluation-coverage"],
              }),
              queryClient.invalidateQueries({ queryKey: ["evaluations"] }),
              ...(selectedEvaluationId
                ? [
                    queryClient.invalidateQueries({
                      queryKey: ["evaluation", selectedEvaluationId],
                    }),
                  ]
                : []),
            ]);
          }}
        >
          刷新
        </Button>
      }
    >
      {contextualQueryError && (
        <Alert
          type="error"
          showIcon
          className="block-gap-bottom"
          content={`当前评测数据读取失败：${formatApiError(contextualQueryError)}`}
        />
      )}
      {loading ? (
        <Spin className="overview-spinner" />
      ) : (
        <Tabs
          activeTab={tab}
          onChange={(nextTab) => {
            casePreviewRequest.current.invalidate();
            setCasePreview(null);
            setTab(nextTab);
          }}
          destroyOnHide={false}
        >
          <Tabs.TabPane key="create" title="新建评测">
            <CreateEvaluationPane
              bots={bots}
              profiles={profiles}
              suites={suites}
              suiteCases={suiteCasesQuery.data ?? []}
              suiteCasesLoading={suiteCasesQuery.isLoading}
              form={form}
              selectedProfile={selectedProfile}
              selectedSuite={selectedSuite}
              activeForBot={activeForBot}
              recordsReady={recordsQuery.isSuccess}
              recordsError={
                recordsQuery.error
                  ? formatApiError(recordsQuery.error)
                  : ""
              }
              problem={startProblem}
              starting={startMutation.isPending}
              onChange={updateForm}
              onStart={startEvaluation}
              onPrepare={(suite) => void prepareSuite(suite)}
              onPreviewCase={(item) => void openCasePreview(form.suiteId, item)}
            />
          </Tabs.TabPane>
          <Tabs.TabPane key="records" title="评测记录">
            <RecordsPane
              bots={bots}
              records={filteredRecords}
              loading={recordsQuery.isLoading}
              error={
                recordsQuery.error
                  ? formatApiError(recordsQuery.error)
                  : ""
              }
              kind={recordKind}
              botId={recordBot}
              status={recordStatus}
              onKindChange={setRecordKind}
              onBotChange={setRecordBot}
              onStatusChange={setRecordStatus}
              onOpen={(record) => selectEvaluation(record.evaluation_id)}
              onCancel={cancelRecord}
              onRerun={(record) => void rerunRecord(record)}
              onDelete={deleteRecord}
            />
          </Tabs.TabPane>
          <Tabs.TabPane key="catalog" title="任务集">
            <CatalogPane
              bots={bots}
              botId={form.botId}
              profiles={profiles}
              suites={suites}
              coverage={coverageQuery.data ?? null}
              coverageLoading={coverageQuery.isLoading}
              selectedSuiteId={catalogSuiteId}
              suiteCases={catalogCasesQuery.data ?? []}
              suiteCasesLoading={catalogCasesQuery.isLoading}
              onBotChange={(botId) => {
                updateForm({ botId });
              }}
              onSelectSuite={(suiteId) => {
                casePreviewRequest.current.invalidate();
                setCasePreview(null);
                setCatalogSuiteId(suiteId);
              }}
              onPrepare={(suite) => void prepareSuite(suite)}
              onPreviewCase={(item) =>
                void openCasePreview(catalogSuiteId, item)
              }
              onCoverageDetail={setCoverageDetail}
            />
          </Tabs.TabPane>
        </Tabs>
      )}

      <EvaluationDetailDrawer
        evaluationId={selectedEvaluationId}
        record={detail}
        loading={detailQuery.isLoading}
        error={
          detailQuery.error
            ? formatApiError(detailQuery.error)
            : ""
        }
        events={events}
        onRetry={() => void detailQuery.refetch()}
        onClose={() => selectEvaluation("")}
        onCancel={() => detail && cancelRecord(detail)}
        onOpenCase={(caseRef) => void openEvaluationCase(caseRef)}
      />
      <CaseEvidenceModal
        value={caseDetail}
        loading={caseDetailLoading}
        onClose={closeCaseDetail}
      />
      <CasePreviewModal
        value={casePreview}
        onClose={closeCasePreview}
      />
      <CoverageHistoryDrawer
        value={coverageDetail}
        onClose={() => setCoverageDetail(null)}
      />
      <TaskStreamSheet
        title={prepareStream.title}
        visible={prepareStream.open}
        running={prepareStream.running}
        lines={prepareStream.lines}
        onClose={prepareStream.close}
      />
    </PageSection>
  );
}

function CreateEvaluationPane({
  bots,
  profiles,
  suites,
  suiteCases,
  suiteCasesLoading,
  form,
  selectedProfile,
  selectedSuite,
  activeForBot,
  recordsReady,
  recordsError,
  problem,
  starting,
  onChange,
  onStart,
  onPrepare,
  onPreviewCase,
}: {
  bots: BotInstance[];
  profiles: EvaluationProfile[];
  suites: EvaluationSuite[];
  suiteCases: EvaluationCaseSummary[];
  suiteCasesLoading: boolean;
  form: EvaluationForm;
  selectedProfile: EvaluationProfile | null;
  selectedSuite: EvaluationSuite | null;
  activeForBot: EvaluationRecord | undefined;
  recordsReady: boolean;
  recordsError: string;
  problem: ApiProblem | null;
  starting: boolean;
  onChange: (patch: Partial<EvaluationForm>) => void;
  onStart: () => void;
  onPrepare: (suite: EvaluationSuite) => void;
  onPreviewCase: (item: EvaluationCaseSummary) => void;
}) {
  const [caseQuery, setCaseQuery] = useState("");
  const filteredCases = useMemo(() => {
    const query = caseQuery.trim().toLowerCase();
    if (!query) return suiteCases;
    return suiteCases.filter((item) =>
      `${item.case_id} ${item.category} ${item.summary}`
        .toLowerCase()
        .includes(query),
    );
  }, [caseQuery, suiteCases]);
  const filteredCaseIds = filteredCases.map((item) => item.case_id);
  const declaredSuitePresets = (selectedSuite?.presets ?? [])
    .map((item) => item.preset_id)
    .filter((value): value is SuitePreset =>
      ["quick", "full", "security", "qq-live", "custom"].includes(value),
    );
  const availableSuitePresets = declaredSuitePresets.length
    ? declaredSuitePresets
    : ["custom" as SuitePreset];
  const requiresExternalWrite = suitePresetRequiresExternalWrite(
    selectedSuite,
    form.suitePreset,
    form.caseIds,
  );
  const allFilteredSelected =
    filteredCaseIds.length > 0 &&
    filteredCaseIds.every((caseId) => form.caseIds.includes(caseId));

  const toggleFiltered = () => {
    if (allFilteredSelected) {
      const selected = new Set(filteredCaseIds);
      onChange({ caseIds: form.caseIds.filter((caseId) => !selected.has(caseId)) });
      return;
    }
    onChange({ caseIds: Array.from(new Set([...form.caseIds, ...filteredCaseIds])) });
  };

  const caseColumns: ColumnProps<EvaluationCaseSummary>[] = [
    {
      title: "Case",
      dataIndex: "case_id",
      width: 210,
      render: (value: string, record) => (
        <Button type="text" size="small" onClick={() => onPreviewCase(record)}>
          {value}
        </Button>
      ),
    },
    { title: "分类", dataIndex: "category", width: 150 },
    { title: "题目简述", dataIndex: "summary", ellipsis: true },
  ];

  const startDisabled =
    !form.botId ||
    !recordsReady ||
    Boolean(activeForBot) ||
    (form.kind === "comparison"
      ? !form.profileId ||
        (form.preset === "custom" &&
          (!form.targetIds.length || !form.caseRefs.length))
      : !selectedSuite?.ready ||
        (form.suitePreset === "custom" && !form.caseIds.length) ||
        (requiresExternalWrite && !form.confirmExternalWrite));

  return (
    <div className="eval-center-stack">
      <Card className="eval-create-card">
        <div className="eval-create-header">
          <label>
            <Text bold>Bot</Text>
            <Select
              value={form.botId || undefined}
              placeholder="选择 Bot"
              options={bots.map((bot) => ({
                label: bot.display_name,
                value: bot.instance_id,
              }))}
              onChange={(value) =>
                onChange({
                  botId: String(value ?? ""),
                  suiteId: "",
                  caseIds: [],
                  suitePreset: "custom",
                  confirmExternalWrite: false,
                  dryRun: false,
                  llmJudge: false,
                })
              }
            />
          </label>
          <label>
            <Text bold>评测类型</Text>
            <Radio.Group
              type="button"
              value={form.kind}
              onChange={(value) => onChange({ kind: value as EvaluationKind })}
            >
              <Radio value="comparison">Agent 对比</Radio>
              <Radio value="suite">能力 / Suite</Radio>
            </Radio.Group>
          </label>
        </div>
      </Card>

      {activeForBot && (
        <Alert
          type="warning"
          showIcon
          content={`该 Bot 已有活动评测 ${activeForBot.evaluation_id}；完成或取消后才能创建下一条。`}
        />
      )}
      {!recordsReady && (
        <Alert
          type={recordsError ? "error" : "info"}
          showIcon
          content={
            recordsError
              ? `无法读取活动评测：${recordsError}。为避免重复执行，启动已禁用。`
              : "正在确认该 Bot 是否已有活动评测…"
          }
        />
      )}

      {form.kind === "comparison" ? (
        <Card title="Agent 对比配置" className="eval-create-card">
          <div className="eval-field-grid">
            <label>
              <Text bold>Profile</Text>
              <Select
                value={form.profileId || undefined}
                options={profiles.map((profile) => ({
                  label: profile.name,
                  value: profile.profile_id,
                }))}
                onChange={(value) => onChange({ profileId: String(value ?? "") })}
              />
            </label>
            <label>
              <Text bold>策略</Text>
              <Radio.Group
                type="button"
                value={form.preset}
                onChange={(value) =>
                  onChange({ preset: value as ComparisonPreset })
                }
              >
                <Radio value="quick">Quick</Radio>
                <Radio value="standard">Standard</Radio>
                <Radio value="custom">Custom</Radio>
              </Radio.Group>
              <Text type="secondary">
                {presetDescription(selectedProfile, form.preset)}
              </Text>
            </label>
          </div>
          {selectedProfile && (
            <Alert
              type="info"
              className="eval-scope-alert"
              content={`${selectedProfile.description} · ${selectedProfile.cases.length} Cases · ${selectedProfile.dimensions.length} 维度`}
            />
          )}
          {form.preset === "custom" && (
            <div className="eval-custom-panel">
              <div className="eval-field-grid">
                <label>
                  <Text bold>Target</Text>
                  <Checkbox.Group
                    value={form.targetIds}
                    options={[
                      { label: "Codex", value: "codex" },
                      { label: "Native", value: "native" },
                    ]}
                    onChange={(values) =>
                      onChange({ targetIds: values.map(String) })
                    }
                  />
                </label>
                <label>
                  <Text bold>重复次数</Text>
                  <InputNumber
                    min={1}
                    max={10}
                    precision={0}
                    value={form.repetitions}
                    onChange={(value) =>
                      onChange({ repetitions: Number(value ?? 1) })
                    }
                  />
                </label>
                <label>
                  <Text bold>时间预算（秒）</Text>
                  <InputNumber
                    min={30}
                    max={21600}
                    precision={0}
                    value={form.maxWallSeconds}
                    onChange={(value) =>
                      onChange({ maxWallSeconds: Number(value ?? 2700) })
                    }
                  />
                </label>
                <label>
                  <Text bold>Seed</Text>
                  <InputNumber
                    precision={0}
                    value={form.seed}
                    onChange={(value) => onChange({ seed: Number(value ?? 0) })}
                  />
                </label>
              </div>
              <div className="eval-case-picker">
                <Text bold>
                  Profile Case（{form.caseRefs.length}/{selectedProfile?.cases.length ?? 0}）
                </Text>
                <Checkbox.Group
                  value={form.caseRefs}
                  onChange={(values) => onChange({ caseRefs: values.map(String) })}
                >
                  <Space direction="vertical">
                    {selectedProfile?.cases.map((item) => (
                      <Checkbox key={item.ref} value={item.ref}>
                        {DIMENSION_LABELS[item.dimension] ?? item.dimension} ·{" "}
                        {item.case_id}
                      </Checkbox>
                    ))}
                  </Space>
                </Checkbox.Group>
              </div>
            </div>
          )}
        </Card>
      ) : (
        <Card title="能力 / Suite 手动测评配置" className="eval-create-card">
          <div className="eval-field-grid">
            <label>
              <Text bold>Suite</Text>
              <Select
                value={form.suiteId || undefined}
                placeholder="选择 Suite"
                options={suites.map((suite) => ({
                  label: `${suite.name} · ${suite.ready ? `${suite.case_count} Cases` : "未就绪"}`,
                  value: suite.suite_id,
                }))}
                onChange={(value) =>
                  onChange({
                    suiteId: String(value ?? ""),
                    caseIds: [],
                    confirmExternalWrite: false,
                  })
                }
              />
            </label>
            <label>
              <Text bold>Preset</Text>
              <Select
                value={form.suitePreset}
                options={availableSuitePresets.map((preset) => ({
                  label: preset,
                  value: preset,
                }))}
                onChange={(value) =>
                  onChange({
                    suitePreset: value as SuitePreset,
                    caseIds: [],
                    confirmExternalWrite: false,
                  })
                }
              />
            </label>
            <label>
              <Text bold>重复次数</Text>
              <InputNumber
                min={1}
                max={10}
                precision={0}
                value={form.suiteRepetitions}
                onChange={(value) =>
                  onChange({ suiteRepetitions: Number(value ?? 1) })
                }
              />
            </label>
            <label>
              <Text bold>总时间预算（秒）</Text>
              <InputNumber
                min={0}
                max={21600}
                precision={0}
                value={form.suiteMaxWallSeconds}
                onChange={(value) =>
                  onChange({ suiteMaxWallSeconds: Number(value ?? 0) })
                }
              />
            </label>
            <label>
              <Text bold>Seed</Text>
              <InputNumber
                precision={0}
                value={form.suiteSeed}
                onChange={(value) =>
                  onChange({ suiteSeed: Number(value ?? 0) })
                }
              />
            </label>
            <label>
              <Text bold>执行选项</Text>
              <Space wrap>
                <Checkbox
                  checked={form.dryRun}
                  onChange={(checked) => onChange({ dryRun: Boolean(checked) })}
                >
                  Dry run
                </Checkbox>
                {suiteSupportsLlmJudge(selectedSuite) && (
                  <Checkbox
                    checked={form.llmJudge}
                    onChange={(checked) =>
                      onChange({ llmJudge: Boolean(checked) })
                    }
                  >
                    GAIA LLM Judge
                  </Checkbox>
                )}
              </Space>
            </label>
          </div>
          {selectedSuite && (
            <Descriptions
              size="small"
              className="eval-suite-description"
              data={[
                { label: "用途", value: selectedSuite.value || "—" },
                { label: "建议", value: selectedSuite.recommendation || "—" },
                {
                  label: "数据",
                  value: selectedSuite.ready
                    ? `${selectedSuite.case_count} Cases · ${selectedSuite.data_source ?? "ready"}`
                    : selectedSuite.unavailable_reason || "未就绪",
                },
                {
                  label: "状态 / Driver",
                  value: `${selectedSuite.capability_status ?? selectedSuite.status ?? (selectedSuite.ready ? "ready" : "unavailable")} · ${selectedSuite.execution_scope ?? selectedSuite.driver_id ?? selectedSuite.driver ?? "—"}`,
                },
              ]}
            />
          )}
          {selectedSuite && !selectedSuite.ready && (
            <Alert
              type="warning"
              showIcon
              content={selectedSuite.unavailable_reason || "该 Suite 尚未就绪"}
              action={
                selectedSuite.prepare_available ? (
                  <Button size="small" onClick={() => onPrepare(selectedSuite)}>
                    准备数据
                  </Button>
                ) : undefined
              }
            />
          )}
          {requiresExternalWrite && (
            <Alert
              type="warning"
              showIcon
              title="本次评测会产生真实 QQ 外部写入"
              content={
                <Checkbox
                  checked={form.confirmExternalWrite}
                  onChange={(checked) =>
                    onChange({ confirmExternalWrite: Boolean(checked) })
                  }
                >
                  我确认本次所选 Case 可以向固定测试账号和测试群发送消息
                </Checkbox>
              }
            />
          )}
          {form.suitePreset === "custom" ? (
            <>
              <div className="eval-case-toolbar">
                <Input
                  value={caseQuery}
                  allowClear
                  placeholder="搜索 Case ID、分类或题目"
                  onChange={setCaseQuery}
                />
                <Button
                  disabled={!filteredCaseIds.length}
                  onClick={toggleFiltered}
                >
                  {allFilteredSelected ? "取消当前结果" : "全选当前结果"}
                </Button>
                <Tag color="blue">已选 {form.caseIds.length}</Tag>
              </div>
              <Table<EvaluationCaseSummary>
                rowKey="case_id"
                size="small"
                data={filteredCases}
                columns={caseColumns}
                loading={suiteCasesLoading}
                pagination={{ pageSize: 8 }}
                rowSelection={{
                  preserveSelectedRowKeys: true,
                  selectedRowKeys: form.caseIds,
                  onChange: (keys) => onChange({ caseIds: keys.map(String) }),
                }}
                noDataElement={
                  selectedSuite?.ready ? "没有匹配的 Case" : "请选择已就绪的 Suite"
                }
              />
            </>
          ) : (
            <Alert
              type="info"
              showIcon
              content={`由 ${form.suitePreset} Preset 固定选择 ${selectedSuite?.presets?.find((item) => item.preset_id === form.suitePreset)?.case_ids.length ?? "预定义"} 个 Case；如需手选请切换到 custom。`}
            />
          )}
        </Card>
      )}

      {problem && <InlineProblem value={problem} />}
      <div className="eval-primary-action">
        <Button
          type="primary"
          size="large"
          loading={starting}
          disabled={startDisabled}
          onClick={onStart}
        >
          开始评测
        </Button>
        <Text type="secondary">
          创建接口会先完成无副作用校验；阻断时不会生成记录或启动进程。
        </Text>
      </div>
    </div>
  );
}

function InlineProblem({ value }: { value: ApiProblem }) {
  return (
    <Alert
      type="error"
      showIcon
      title={value.code}
      content={
        <div className="eval-problem">
          <div>{value.message}</div>
          {value.checks.length > 0 && (
            <div className="eval-preflight-list">
              {value.checks.map((check) => (
                <div key={check.code} className="eval-preflight-row">
                  <Tag color={check.ok ? "green" : "red"}>
                    {check.ok ? "PASS" : "BLOCK"}
                  </Tag>
                  <div>
                    <Text bold>{check.label}</Text>
                    {check.detail && <div>{check.detail}</div>}
                    {!check.ok && check.action && (
                      <Text type="secondary">处理：{check.action}</Text>
                    )}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      }
    />
  );
}

function RecordsPane({
  bots,
  records,
  loading,
  error,
  kind,
  botId,
  status,
  onKindChange,
  onBotChange,
  onStatusChange,
  onOpen,
  onCancel,
  onRerun,
  onDelete,
}: {
  bots: BotInstance[];
  records: EvaluationRecord[];
  loading: boolean;
  error: string;
  kind: string;
  botId: string;
  status: string;
  onKindChange: (value: string) => void;
  onBotChange: (value: string) => void;
  onStatusChange: (value: string) => void;
  onOpen: (record: EvaluationRecord) => void;
  onCancel: (record: EvaluationRecord) => void;
  onRerun: (record: EvaluationRecord) => void;
  onDelete: (record: EvaluationRecord) => void;
}) {
  const columns: ColumnProps<EvaluationRecord>[] = [
    {
      title: "Evaluation",
      dataIndex: "evaluation_id",
      width: 245,
      render: (value: string, record) => (
        <Button type="text" size="small" onClick={() => onOpen(record)}>
          {value}
        </Button>
      ),
    },
    {
      title: "类型",
      dataIndex: "kind",
      width: 110,
      render: (value: EvaluationKind) => kindTag(value),
    },
    { title: "Bot", dataIndex: "bot_id", width: 180 },
    {
      title: "任务集",
      width: 190,
      render: (_: unknown, record) => evaluationLabel(record),
    },
    {
      title: "Target",
      width: 180,
      render: (_: unknown, record) =>
        record.targets.length
          ? record.targets.map((target) => (
              <Tag key={`${target.target_id}:${target.fingerprint}`}>
                {target.label}
              </Tag>
            ))
          : "—",
    },
    {
      title: "状态",
      dataIndex: "status",
      width: 110,
      render: (value: string) => lifecycleTag(value),
    },
    {
      title: "进度",
      width: 130,
      render: (_: unknown, record) =>
        record.progress.total
          ? `${record.progress.completed}/${record.progress.total} · ${record.progress.percent}%`
          : `${record.progress.completed}`,
    },
    {
      title: "开始时间",
      width: 180,
      render: (_: unknown, record) =>
        formatTime(record.started_at || record.created_at),
    },
    {
      title: "操作",
      width: 230,
      fixed: "right",
      render: (_: unknown, record) => (
        <Space>
          <Button size="mini" onClick={() => onOpen(record)}>详情</Button>
          {ACTIVE_STATUSES.has(record.status) ? (
            <Button size="mini" status="danger" onClick={() => onCancel(record)}>
              取消
            </Button>
          ) : (
            <>
              <Button size="mini" onClick={() => onRerun(record)}>重跑</Button>
              <Button
                size="mini"
                type="text"
                status="danger"
                onClick={() => onDelete(record)}
              >
                删除
              </Button>
            </>
          )}
        </Space>
      ),
    },
  ];

  return (
    <div className="eval-center-stack">
      {error && (
        <Alert
          type="error"
          showIcon
          content={`评测记录读取失败：${error}`}
        />
      )}
      <Space wrap className="eval-filter-bar">
        <Select
          allowClear
          placeholder="全部类型"
          style={{ width: 150 }}
          value={kind || undefined}
          options={[
            { label: "Agent 对比", value: "comparison" },
            { label: "基准评测", value: "suite" },
          ]}
          onChange={(value) => onKindChange(String(value ?? ""))}
        />
        <Select
          allowClear
          placeholder="全部 Bot"
          style={{ width: 210 }}
          value={botId || undefined}
          options={bots.map((bot) => ({
            label: bot.display_name,
            value: bot.instance_id,
          }))}
          onChange={(value) => onBotChange(String(value ?? ""))}
        />
        <Select
          allowClear
          placeholder="全部状态"
          style={{ width: 170 }}
          value={status || undefined}
          options={[
            "queued",
            "running",
            "completed",
            "partial",
            "cancelled",
            "interrupted",
            "error",
          ].map((value) => ({ label: value, value }))}
          onChange={(value) => onStatusChange(String(value ?? ""))}
        />
      </Space>
      <Table<EvaluationRecord>
        rowKey="evaluation_id"
        data={records}
        columns={columns}
        loading={loading}
        pagination={{ pageSize: 10 }}
        scroll={{ x: 1420 }}
        noDataElement={<Empty description="暂无评测记录" />}
      />
    </div>
  );
}

function EvaluationDetailDrawer({
  evaluationId,
  record,
  loading,
  error,
  events,
  onRetry,
  onClose,
  onCancel,
  onOpenCase,
}: {
  evaluationId: string;
  record: EvaluationRecord | null;
  loading: boolean;
  error: string;
  events: Array<Record<string, unknown>>;
  onRetry: () => void;
  onClose: () => void;
  onCancel: () => void;
  onOpenCase: (caseRef: string) => void;
}) {
  return (
    <Drawer
      title={evaluationId ? `评测详情 · ${evaluationId}` : "评测详情"}
      visible={Boolean(evaluationId)}
      width="min(1080px, calc(100vw - 24px))"
      onCancel={onClose}
      footer={null}
    >
      {loading && !record ? (
        <Spin />
      ) : error && !record ? (
        <Alert
          type="error"
          showIcon
          content={`详情读取失败：${error}`}
          action={<Button size="small" onClick={onRetry}>重试</Button>}
        />
      ) : record ? (
        <div className="eval-center-stack">
          {error && (
            <Alert
              type="warning"
              showIcon
              content={`刷新失败，当前展示上次成功数据：${error}`}
              action={<Button size="small" onClick={onRetry}>重试</Button>}
            />
          )}
          <Descriptions
            size="small"
            data={[
              { label: "类型", value: kindTag(record.kind) },
              { label: "生命周期", value: lifecycleTag(record.status) },
              { label: "Bot", value: record.bot_id },
              { label: "任务集", value: evaluationLabel(record) },
              {
                label: "开始 / 耗时",
                value: `${formatTime(record.started_at || record.created_at)} / ${formatDuration(record.duration_seconds)}`,
              },
            ]}
          />
          <Progress
            percent={record.progress.percent}
            showText
            animation={ACTIVE_STATUSES.has(record.status)}
          />
          <Space wrap>
            <Text type="secondary">
              {record.progress.completed}/{record.progress.total || "?"}
              {record.progress.current ? ` · ${record.progress.current}` : ""}
            </Text>
            {ACTIVE_STATUSES.has(record.status) ? (
              <Button size="small" status="danger" onClick={onCancel}>
                取消
              </Button>
            ) : record.result ? (
              <>
                <a href={evaluationExportUrl(record.evaluation_id, "json")}>
                  <Button size="small">导出 JSON</Button>
                </a>
                <a href={evaluationExportUrl(record.evaluation_id, "markdown")}>
                  <Button size="small">导出 Markdown</Button>
                </a>
              </>
            ) : null}
          </Space>
          {record.error && <Alert type="error" showIcon content={record.error} />}
          <TargetSnapshot targets={record.targets} />
          {record.result ? (
            record.kind === "comparison" ? (
              <ComparisonResult
                record={record}
                onOpenCase={onOpenCase}
              />
            ) : (
              <SuiteResult record={record} onOpenCase={onOpenCase} />
            )
          ) : (
            !ACTIVE_STATUSES.has(record.status) && (
              <Empty description="该记录尚无持久化结果" />
            )
          )}
          {(ACTIVE_STATUSES.has(record.status) || events.length > 0) && (
            <Collapse>
              <Collapse.Item
                name="events"
                header={`结构化进度事件（${events.length}）`}
              >
                <pre className="eval-event-log">
                  {events.map((event) => JSON.stringify(event)).join("\n") ||
                    "等待事件…"}
                </pre>
              </Collapse.Item>
            </Collapse>
          )}
        </div>
      ) : null}
    </Drawer>
  );
}

function TargetSnapshot({ targets }: { targets: EvaluationRecord["targets"] }) {
  if (!targets.length) return null;
  return (
    <Card size="small" title="Target 快照">
      <div className="eval-target-grid">
        {targets.map((target) => (
          <div
            key={`${target.target_id}:${target.fingerprint}`}
            className="eval-target-card"
          >
            <Space wrap>
              <Text bold>{target.label}</Text>
              {target.backend && <Tag>{target.backend}</Tag>}
              {target.executor && <Tag>{target.executor}</Tag>}
            </Space>
            <Text type="secondary">
              {target.model || "model unknown"}
              {target.reasoning_effort
                ? ` · ${target.reasoning_effort}`
                : ""}
            </Text>
            <Text
              type="secondary"
              title={target.fingerprint || "fingerprint unavailable"}
            >
              {formatFingerprint(target.fingerprint)}
            </Text>
          </div>
        ))}
      </div>
    </Card>
  );
}

function ComparisonResult({
  record,
  onOpenCase,
}: {
  record: EvaluationRecord;
  onOpenCase: (caseRef: string) => void;
}) {
  const result = record.result ?? {};
  const summary = recordValue(result.summary);
  const wins = recordValue(summary.wins);
  const comparisons = recordArray(result.comparisons);
  const dimensions = recordValue(result.dimensions);
  const dimensionEntries = Object.entries(dimensions).filter(([, value]) =>
    isRecord(value),
  );
  const targetIds = record.targets.map((target) => target.target_id);
  const hasPairedTargets = targetIds.length === 2;
  const columns: ColumnProps<Record<string, unknown>>[] = [
    {
      title: "Case",
      width: 260,
      render: (_: unknown, item) =>
        stringValue(item.case_ref, stringValue(item.case_id)),
    },
    {
      title: "维度",
      width: 140,
      render: (_: unknown, item) => {
        const dimension = stringValue(item.dimension);
        return DIMENSION_LABELS[dimension] ?? dimension;
      },
    },
    ...targetIds.map(
      (targetId): ColumnProps<Record<string, unknown>> => ({
        title:
          record.targets.find((target) => target.target_id === targetId)?.label ??
          targetId,
        width: 110,
        render: (_: unknown, item) => {
          const targets = recordValue(item.targets);
          return formatScore(recordValue(targets[targetId]).mean_score);
        },
      }),
    ),
    ...(hasPairedTargets
      ? [{
          title: "结果",
          width: 120,
          render: (_: unknown, item: Record<string, unknown>) => {
            const verdict = stringValue(item.verdict);
            return verdict ? verdictTag(verdict) : "—";
          },
        } satisfies ColumnProps<Record<string, unknown>>]
      : []),
    {
      title: "证据",
      width: 90,
      render: (_: unknown, item) => {
        const caseRef = stringValue(item.case_ref, stringValue(item.case_id));
        return (
          <Button size="mini" onClick={() => onOpenCase(caseRef)}>
            查看
          </Button>
        );
      },
    },
  ];

  return (
    <div className="eval-results">
      {hasPairedTargets ? (
        <div className="eval-score-strip">
          {targetIds.map((targetId) => (
            <div key={targetId}>
              <strong>{numberValue(wins[targetId]) ?? 0}</strong>
              <span>
                {record.targets.find((target) => target.target_id === targetId)
                  ?.label ?? targetId} 胜
              </span>
            </div>
          ))}
          <div>
            <strong>{numberValue(summary.ties) ?? 0}</strong>
            <span>平局</span>
          </div>
          <div>
            <strong>{numberValue(summary.inconclusive) ?? 0}</strong>
            <span>结论不足</span>
          </div>
        </div>
      ) : (
        <Alert
          type="info"
          content="单 Target 评测只展示分数与证据，不生成胜负、平局或排名。"
        />
      )}
      {dimensionEntries.length > 0 && (
        <div className="eval-matrix-grid">
          {dimensionEntries.map(([dimension, raw]) => {
            const item = recordValue(raw);
            const targets = recordValue(item.targets);
            const verdict = stringValue(item.verdict);
            return (
              <div
                key={dimension}
                className={`eval-matrix-card eval-verdict-${verdict || "inconclusive"}`}
              >
                <Text bold>{DIMENSION_LABELS[dimension] ?? dimension}</Text>
                <div className="eval-matrix-scores">
                  {targetIds.map((targetId) => (
                    <span key={targetId}>
                      {record.targets.find((target) => target.target_id === targetId)
                        ?.label ?? targetId}{" "}
                      <b>{formatScore(recordValue(targets[targetId]).mean_score)}</b>
                    </span>
                  ))}
                </div>
                {hasPairedTargets && verdict
                  ? verdictTag(verdict)
                  : <Text type="secondary">无对比结论</Text>}
              </div>
            );
          })}
        </div>
      )}
      <Table<Record<string, unknown>>
        rowKey={(item) =>
          stringValue(item.case_ref, stringValue(item.case_id))
        }
        data={comparisons}
        columns={columns}
        pagination={false}
        scroll={{ x: 780 + targetIds.length * 110 }}
        noDataElement={<Empty description="单 Target 或尚无完整配对，不生成胜负结果" />}
      />
    </div>
  );
}

function SuiteResult({
  record,
  onOpenCase,
}: {
  record: EvaluationRecord;
  onOpenCase: (caseRef: string) => void;
}) {
  const result = record.result ?? {};
  const summary = recordValue(result.summary);
  const trials = recordArray(result.trials);
  const isProductCapability = isProductCapabilityEvaluation(record);
  const product = isProductCapability
    ? productCapabilityResultView(record)
    : null;
  const columns: ColumnProps<Record<string, unknown>>[] = [
    {
      title: "Case",
      width: 260,
      render: (_: unknown, item) =>
        stringValue(item.case_ref, stringValue(item.case_id)),
    },
    {
      title: "结果",
      width: 110,
      render: (_: unknown, item) =>
        outcomeTag(stringValue(item.outcome, "—")),
    },
    ...(isProductCapability
      ? []
      : [{
          title: "得分",
          width: 130,
          render: (_: unknown, item: Record<string, unknown>) =>
            formatScore(item.score, item.max_score),
        } satisfies ColumnProps<Record<string, unknown>>]),
    {
      title: "耗时",
      width: 100,
      render: (_: unknown, item) =>
        formatDuration(numberValue(item.duration_seconds)),
    },
    {
      title: "证据",
      width: 90,
      render: (_: unknown, item) => {
        const caseRef = stringValue(item.case_ref, stringValue(item.case_id));
        return (
          <Button size="mini" onClick={() => onOpenCase(caseRef)}>
            查看
          </Button>
        );
      },
    },
  ];
  const capabilityColumns: ColumnProps<ProductCapabilityGroup>[] = [
    {
      title: "能力族",
      dataIndex: "capability",
      width: 240,
      render: (value: string) => CAPABILITY_LABELS[value] ?? value,
    },
    { title: "Case", dataIndex: "total", width: 90 },
    { title: "通过", dataIndex: "passed", width: 90 },
    { title: "失败", dataIndex: "failed", width: 90 },
    { title: "基础设施错误", dataIndex: "errors", width: 130 },
    { title: "跳过", dataIndex: "skipped", width: 90 },
  ];
  const usageEntries = Object.entries(product?.usageTotals ?? {});
  const costEntries = product?.costEntries ?? [];

  return (
    <div className="eval-results">
      {product && (
        <>
          <Card size="small" title="产品能力判定">
            <Space direction="vertical" size="small">
              <Space wrap>
                <Text bold>总体判定</Text>
                {verdictTag(product.verdict)}
                <Tag color="gray">不生成 Agent 智力总分</Tag>
              </Space>
              <Text type="secondary">{product.scoreScope}</Text>
            </Space>
          </Card>
          <div className="eval-score-strip">
            {[
              ["passed", "通过"],
              ["failed", "失败"],
              ["errors", "错误"],
              ["skipped", "跳过"],
            ].map(([field, label]) => (
              <div key={field}>
                <strong>{numberValue(summary[field]) ?? 0}</strong>
                <span>{label}</span>
              </div>
            ))}
            <div>
              <strong>{product.criticalViolations}</strong>
              <span>Critical 违反</span>
            </div>
            <div>
              <strong>{product.infrastructureErrors}</strong>
              <span>基础设施错误</span>
            </div>
          </div>
          <Alert
            type="warning"
            showIcon
            content={`可靠性说明：${product.reliabilityNote}`}
          />
          <Alert
            type="warning"
            showIcon
            content={`模型漂移说明：${product.modelVersionNote}`}
          />
          <Card size="small" title="能力族结果">
            <Table<ProductCapabilityGroup>
              rowKey="capability"
              data={product.capabilities}
              columns={capabilityColumns}
              pagination={false}
              noDataElement={<Empty description="暂无能力族汇总" />}
            />
          </Card>
          {(usageEntries.length > 0 || costEntries.length > 0) && (
            <Card size="small" title="模型用量与成本">
              <Descriptions
                size="small"
                column={2}
                data={[
                  ...usageEntries.map(([label, value]) => ({
                    label,
                    value: String(value),
                  })),
                  ...costEntries.map((item) => ({
                    label: item.label,
                    value: item.value,
                  })),
                ]}
              />
            </Card>
          )}
        </>
      )}
      {!product && (
        <div className="eval-score-strip">
          {[
            ["passed", "通过"],
            ["failed", "失败"],
            ["errors", "错误"],
            ["skipped", "跳过"],
          ].map(([field, label]) => (
            <div key={field}>
              <strong>{numberValue(summary[field]) ?? 0}</strong>
              <span>{label}</span>
            </div>
          ))}
        </div>
      )}
      <Table<Record<string, unknown>>
        rowKey={(item) =>
          stringValue(item.trial_id, stringValue(item.case_ref, stringValue(item.case_id)))
        }
        data={trials}
        columns={columns}
        pagination={{ pageSize: 10 }}
        noDataElement={<Empty description="暂无 Case 结果" />}
      />
    </div>
  );
}

function CaseEvidenceModal({
  value,
  loading,
  onClose,
}: {
  value: EvaluationCaseDetail | null;
  loading: boolean;
  onClose: () => void;
}) {
  return (
    <Modal
      title={value ? `Case 证据 · ${value.case_ref}` : "Case 证据"}
      visible={Boolean(value) || loading}
      footer={null}
      style={{ width: "min(920px, calc(100vw - 24px))" }}
      onCancel={onClose}
    >
      {loading ? (
        <Spin />
      ) : value ? (
        <div className="eval-center-stack">
          {value.comparison && (
            <pre className="eval-json">
              {JSON.stringify(value.comparison, null, 2)}
            </pre>
          )}
          {value.trials.map((trial) => (
            <Collapse key={trial.trial_id || `${trial.case_id}:${trial.target_id}:${trial.attempt}`}>
              <Collapse.Item
                name={trial.trial_id || `${trial.target_id}:${trial.attempt}`}
                header={`${trial.target_id || "Target"} · attempt ${trial.attempt} · ${trial.outcome}`}
              >
                <Descriptions
                  size="small"
                  data={[
                    { label: "结果", value: outcomeTag(trial.outcome) },
                    { label: "得分", value: formatScore(trial.score, trial.max_score) },
                    { label: "耗时", value: formatDuration(trial.duration_seconds) },
                    { label: "停止原因", value: trial.stop_reason || "—" },
                  ]}
                />
                {trial.error && <Alert type="error" content={trial.error} />}
                <Title heading={6}>最终回答</Title>
                <Paragraph className="eval-answer-block">
                  {trial.final_text || "—"}
                </Paragraph>
                {trial.judge && (
                  <>
                    <Title heading={6}>Judge</Title>
                    <pre className="eval-json">
                      {JSON.stringify(trial.judge, null, 2)}
                    </pre>
                  </>
                )}
                {Object.keys(trial.evidence ?? {}).length > 0 && (
                  <>
                    <Title heading={6}>Evidence</Title>
                    <pre className="eval-json">
                      {JSON.stringify(trial.evidence, null, 2)}
                    </pre>
                  </>
                )}
              </Collapse.Item>
            </Collapse>
          ))}
        </div>
      ) : null}
    </Modal>
  );
}

function CatalogPane({
  bots,
  botId,
  profiles,
  suites,
  coverage,
  coverageLoading,
  selectedSuiteId,
  suiteCases,
  suiteCasesLoading,
  onBotChange,
  onSelectSuite,
  onPrepare,
  onPreviewCase,
  onCoverageDetail,
}: {
  bots: BotInstance[];
  botId: string;
  profiles: EvaluationProfile[];
  suites: EvaluationSuite[];
  coverage: EvaluationCoverage | null;
  coverageLoading: boolean;
  selectedSuiteId: string;
  suiteCases: EvaluationCaseSummary[];
  suiteCasesLoading: boolean;
  onBotChange: (botId: string) => void;
  onSelectSuite: (suiteId: string) => void;
  onPrepare: (suite: EvaluationSuite) => void;
  onPreviewCase: (item: EvaluationCaseSummary) => void;
  onCoverageDetail: (record: EvaluationCoverageRecord) => void;
}) {
  const [coverageSearch, setCoverageSearch] = useState("");
  const [coverageTarget, setCoverageTarget] = useState("");
  const coverageRecords = coverage?.records ?? [];
  const targetOptions = useMemo(
    () =>
      Array.from(
        new Set(
          coverageRecords
            .map((item) => item.target_fingerprint)
            .filter(Boolean),
        ),
      ).sort(),
    [coverageRecords],
  );
  useEffect(() => {
    setCoverageSearch("");
    setCoverageTarget("");
  }, [botId]);
  useEffect(() => {
    setCoverageTarget((current) =>
      retainAvailableSelection(current, targetOptions),
    );
  }, [targetOptions]);
  const filteredCoverage = useMemo(() => {
    const search = coverageSearch.trim().toLowerCase();
    return coverageRecords.filter((item) => {
      if (coverageTarget && item.target_fingerprint !== coverageTarget) return false;
      if (!search) return true;
      return `${item.case_ref} ${item.case_id} ${item.suite_id} ${item.bot_id} ${item.target_id}`
        .toLowerCase()
        .includes(search);
    });
  }, [coverageRecords, coverageSearch, coverageTarget]);

  const profileColumns: ColumnProps<EvaluationProfile>[] = [
    { title: "Profile", dataIndex: "name", width: 220 },
    { title: "说明", dataIndex: "description", ellipsis: true },
    {
      title: "Case",
      width: 90,
      render: (_: unknown, profile) => profile.cases.length,
    },
    {
      title: "维度",
      width: 240,
      render: (_: unknown, profile) => (
        <Space wrap>
          {profile.dimensions.map((dimension) => (
            <Tag key={dimension}>
              {DIMENSION_LABELS[dimension] ?? dimension}
            </Tag>
          ))}
        </Space>
      ),
    },
  ];
  const suiteColumns: ColumnProps<EvaluationSuite>[] = [
    { title: "Suite", dataIndex: "name", width: 180 },
    { title: "用途", dataIndex: "value", ellipsis: true },
    {
      title: "执行",
      width: 210,
      render: (_: unknown, suite) => (
        <Space wrap>
          <Tag>{suite.execution_scope ?? suite.driver_id ?? suite.driver ?? "—"}</Tag>
          <Tag color={suite.status === "planned" ? "orange" : "blue"}>
            {suite.capability_status ?? suite.status ?? (suite.ready ? "ready" : "unavailable")}
          </Tag>
        </Space>
      ),
    },
    {
      title: "Preset",
      width: 220,
      render: (_: unknown, suite) => (
        <Space wrap>
          {(suite.presets ?? []).map((preset) => (
            <Tag key={preset.preset_id}>{preset.preset_id}</Tag>
          ))}
          {!suite.presets?.length && <Text type="secondary">custom</Text>}
        </Space>
      ),
    },
    {
      title: "数据",
      width: 150,
      render: (_: unknown, suite) =>
        suite.ready
          ? <Tag color="green">{suite.case_count} Cases</Tag>
          : <Tag color="orange">未就绪</Tag>,
    },
    {
      title: "操作",
      width: 190,
      render: (_: unknown, suite) => (
        <Space>
          <Button
            size="mini"
            disabled={!suite.ready}
            onClick={() => onSelectSuite(suite.suite_id)}
          >
            查看 Case
          </Button>
          {suite.prepare_available && (
            <Button size="mini" onClick={() => onPrepare(suite)}>
              准备数据
            </Button>
          )}
        </Space>
      ),
    },
  ];
  const caseColumns: ColumnProps<EvaluationCaseSummary>[] = [
    {
      title: "Case",
      dataIndex: "case_id",
      width: 220,
      render: (value: string, item) => (
        <Button type="text" size="small" onClick={() => onPreviewCase(item)}>
          {value}
        </Button>
      ),
    },
    { title: "分类", dataIndex: "category", width: 160 },
    { title: "题目简述", dataIndex: "summary", ellipsis: true },
  ];
  const coverageColumns: ColumnProps<EvaluationCoverageRecord>[] = [
    { title: "Case", dataIndex: "case_id", width: 210 },
    { title: "Suite", dataIndex: "suite_id", width: 120 },
    {
      title: "Target",
      width: 150,
      render: (_: unknown, item) => item.target_id || "—",
    },
    {
      title: "Fingerprint",
      width: 180,
      render: (_: unknown, item) => (
        <Text title={item.target_fingerprint}>
          {formatFingerprint(item.target_fingerprint)}
        </Text>
      ),
    },
    {
      title: "最近结果",
      width: 110,
      render: (_: unknown, item) => outcomeTag(item.last_outcome || "—"),
    },
    {
      title: "完成次数",
      dataIndex: "completed_count",
      width: 100,
    },
    {
      title: "最近完成",
      width: 180,
      render: (_: unknown, item) => formatTime(item.last_completed_at),
    },
    {
      title: "历史",
      width: 80,
      render: (_: unknown, item) => (
        <Button size="mini" onClick={() => onCoverageDetail(item)}>
          查看
        </Button>
      ),
    },
  ];

  return (
    <div className="eval-center-stack">
      <Card>
        <Space wrap>
          <Text bold>按 Bot 查看数据就绪与 Target 覆盖</Text>
          <Select
            value={botId || undefined}
            style={{ width: 230 }}
            options={bots.map((bot) => ({
              label: bot.display_name,
              value: bot.instance_id,
            }))}
            onChange={(value) => onBotChange(String(value ?? ""))}
          />
        </Space>
      </Card>
      <Card title="Comparison Profiles">
        <Table<EvaluationProfile>
          rowKey="profile_id"
          size="small"
          data={profiles}
          columns={profileColumns}
          pagination={false}
        />
      </Card>
      <Card title="Capability / Benchmark Suites">
        <Table<EvaluationSuite>
          rowKey="suite_id"
          size="small"
          data={suites}
          columns={suiteColumns}
          pagination={false}
        />
      </Card>
      {selectedSuiteId && (
        <Card
          title={`Suite Cases · ${selectedSuiteId}`}
          extra={
            <Button size="small" onClick={() => onSelectSuite("")}>
              收起
            </Button>
          }
        >
          <Table<EvaluationCaseSummary>
            rowKey="case_id"
            size="small"
            data={suiteCases}
            columns={caseColumns}
            loading={suiteCasesLoading}
            pagination={{ pageSize: 10 }}
          />
        </Card>
      )}
      <Card title="Case 覆盖">
        <div className="eval-coverage-metrics">
          <div className="eval-coverage-metric">
            <Text type="secondary">已完成 Case</Text>
            <div className="eval-coverage-metric-value">
              {coverage?.summary.case_count ?? 0}
            </div>
          </div>
          <div className="eval-coverage-metric">
            <Text type="secondary">最近未通过</Text>
            <div className="eval-coverage-metric-value">
              {coverage?.summary.failed_case_count ?? 0}
            </div>
          </div>
          <div className="eval-coverage-metric">
            <Text type="secondary">Target 配置</Text>
            <div className="eval-coverage-metric-value">
              {coverage?.summary.target_count ?? 0}
            </div>
          </div>
        </div>
        <div className="eval-coverage-toolbar">
          <Input
            value={coverageSearch}
            allowClear
            placeholder="搜索 Case、Suite、Target"
            onChange={setCoverageSearch}
          />
          <Select
            value={coverageTarget || undefined}
            allowClear
            placeholder="全部 Target fingerprint"
            options={targetOptions.map((value) => ({
              label: formatFingerprint(value),
              value,
            }))}
            onChange={(value) => setCoverageTarget(String(value ?? ""))}
          />
        </div>
        <Table<EvaluationCoverageRecord>
          rowKey={(item) =>
            `${item.bot_id}:${item.suite_id}:${item.case_id}:${item.target_fingerprint}`
          }
          size="small"
          data={filteredCoverage}
          columns={coverageColumns}
          loading={coverageLoading}
          pagination={{ pageSize: 10 }}
          scroll={{ x: 1150 }}
          noDataElement="暂无覆盖记录"
        />
      </Card>
    </div>
  );
}

function CasePreviewModal({
  value,
  onClose,
}: {
  value: EvaluationCaseDescriptor | null;
  onClose: () => void;
}) {
  return (
    <Modal
      title={value ? `Case · ${value.case_id}` : "Case"}
      visible={Boolean(value)}
      footer={null}
      style={{ width: "min(800px, calc(100vw - 24px))" }}
      onCancel={onClose}
    >
      {value && (
        <div className="eval-center-stack">
          <Descriptions
            size="small"
            data={[
              { label: "分类", value: value.category || "—" },
              {
                label: "附件",
                value: value.attachment_count
                  ? `${value.attachment_count} 个`
                  : "无",
              },
              { label: "预期行为", value: value.expected_behavior || "—" },
            ]}
          />
          <div>
            <Text bold>题目</Text>
            <Paragraph className="eval-answer-block">{value.input}</Paragraph>
          </div>
          {value.context && (
            <div>
              <Text bold>上下文</Text>
              <Paragraph className="eval-answer-block">{value.context}</Paragraph>
            </div>
          )}
          {value.rubric && (
            <div>
              <Text bold>Rubric</Text>
              <Paragraph className="eval-answer-block">{value.rubric}</Paragraph>
            </div>
          )}
        </div>
      )}
    </Modal>
  );
}

function CoverageHistoryDrawer({
  value,
  onClose,
}: {
  value: EvaluationCoverageRecord | null;
  onClose: () => void;
}) {
  const columns: ColumnProps<EvaluationCoverageRecord["history"][number]>[] = [
    { title: "Evaluation", dataIndex: "evaluation_id", width: 250 },
    {
      title: "Attempt",
      dataIndex: "attempt",
      width: 90,
      render: (attempt: number) => attempt || "—",
    },
    {
      title: "结果",
      dataIndex: "outcome",
      width: 110,
      render: (outcome: string) => outcomeTag(outcome),
    },
    {
      title: "得分",
      width: 130,
      render: (_: unknown, item) => formatScore(item.score),
    },
    {
      title: "耗时",
      width: 100,
      render: (_: unknown, item) => formatDuration(item.duration_seconds),
    },
    {
      title: "完成时间",
      dataIndex: "finished_at",
      width: 180,
      render: (finishedAt: string) => formatTime(finishedAt),
    },
  ];
  return (
    <Drawer
      title={value ? `覆盖历史 · ${value.case_id}` : "覆盖历史"}
      visible={Boolean(value)}
      width="min(900px, calc(100vw - 24px))"
      footer={null}
      onCancel={onClose}
    >
      {value && (
        <div className="eval-center-stack">
          <Descriptions
            size="small"
            data={[
              { label: "Bot / Suite", value: `${value.bot_id} / ${value.suite_id}` },
              { label: "Target", value: value.target_id || "—" },
              {
                label: "Fingerprint",
                value: value.target_fingerprint || "—",
              },
              { label: "完成次数", value: value.completed_count },
            ]}
          />
          <Table
            rowKey={(item) =>
              item.trial_id ||
              `${item.evaluation_id}:${item.attempt}:${item.finished_at}`
            }
            data={value.history}
            columns={columns}
            pagination={false}
            scroll={{ x: 780 }}
          />
        </div>
      )}
    </Drawer>
  );
}
