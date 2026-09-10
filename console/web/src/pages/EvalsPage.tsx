import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Button,
  Card,
  Descriptions,
  Drawer,
  Empty,
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

import { api } from "../api";
import {
  evaluationApi,
  evaluationExportUrl,
} from "../features/evals/evaluationApi";
import {
  EvaluationApiError,
  buildSuiteRequest,
  formatApiError,
  type ApiProblem,
  type EvaluationRecord,
  type EvaluationStatus,
  type EvaluationSuite,
  type SuitePreset,
} from "../features/evals/model";
import type { ColumnProps } from "../shared/ui/arcoTypes";
import PageSection from "../shared/ui/PageSection";
import EvaluationTrends from "../features/evals/EvaluationTrends";
import { EvaluationResults } from "../features/evals/EvaluationResults";
import { dateLabel, evaluationSuiteId, modelLabel, rateLabel, revisionLabel } from "../features/evals/insightsModel";

const { Paragraph, Text } = Typography;

type EvaluationTrack = "agent" | "qq_message_flow";

interface Props {
  visible?: boolean;
}

interface TrackDefinition {
  id: EvaluationTrack;
  title: string;
  shortTitle: string;
  description: string;
  framework?: string;
  includes: string;
  excludes: string;
  suiteId: string;
  accent: string;
}

const TRACKS: readonly TrackDefinition[] = [
  {
    id: "agent",
    title: "Agent 评测",
    shortTitle: "Agent 评测",
    description: "评估任务完成、工具使用、多轮交互和协作表现。",
    framework: "DeepEval",
    includes: "工具决策、搜索与证据、记忆与上下文、文件与图片、Skills、子 Agent、代码任务及指令遵循",
    excludes: "QQ 消息接入、网关准入与平台投递",
    suiteId: "agentstrata-capabilities-v1",
    accent: "arcoblue",
  },
  {
    id: "qq_message_flow",
    title: "QQ 消息全链路",
    shortTitle: "QQ 链路",
    description: "假设 QQ 已产生消息，验证 AgentStrata 自有代码能否安全传到回复投影。",
    includes: "合成 OneBot、网关过滤、attestation、身份权限、会话、人格持久化与回复投影",
    excludes: "不连接真实 QQ，不冒充真实 NapCat、cc-connect 或外部用户 E2E",
    suiteId: "agentstrata-qq-message-flow-v1",
    accent: "purple",
  },
] as const;

const ACTIVE_STATUSES = new Set<EvaluationStatus>(["queued", "running"]);
const STATUS_COLORS: Record<string, string> = {
  queued: "gray",
  running: "arcoblue",
  completed: "green",
  partial: "orange",
  cancelled: "gray",
  interrupted: "orangered",
  error: "red",
};

function suiteId(record: EvaluationRecord): string {
  return evaluationSuiteId(record);
}

function trackForRecord(record: EvaluationRecord): TrackDefinition | null {
  const id = suiteId(record);
  return TRACKS.find((track) => track.suiteId === id) ?? null;
}

function formatTime(value: string | null | undefined): string {
  if (!value) return "—";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString("zh-CN");
}

function formatDuration(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "—";
  if (value < 60) return `${value.toFixed(1)} 秒`;
  return `${Math.floor(value / 60)} 分 ${Math.round(value % 60)} 秒`;
}

function presetOptions(suite: EvaluationSuite | null): SuitePreset[] {
  const declared = (suite?.presets ?? [])
    .map((item) => item.preset_id)
    .filter((value): value is SuitePreset =>
      value === "quick" || value === "full" || value === "security"
    );
  return declared.length ? declared : ["quick"];
}

function presetDescription(suite: EvaluationSuite | null, preset: SuitePreset): string {
  return suite?.presets?.find((item) => item.preset_id === preset)?.description ?? "";
}

export default function EvalsPage({ visible = true }: Props) {
  const queryClient = useQueryClient();
  const detailTop = useRef<HTMLDivElement>(null);
  const [tab, setTab] = useState("start");
  const [botId, setBotId] = useState("");
  const [selectedTrack, setSelectedTrack] = useState<EvaluationTrack>("agent");
  const [presets, setPresets] = useState<Record<EvaluationTrack, SuitePreset>>({
    agent: "quick",
    qq_message_flow: "quick",
  });
  const [problem, setProblem] = useState<ApiProblem | null>(null);
  const [selectedEvaluation, setSelectedEvaluation] = useState<EvaluationRecord | null>(null);
  const [recordTrack, setRecordTrack] = useState("all");
  const [recordStatus, setRecordStatus] = useState("all");

  const botsQuery = useQuery({
    queryKey: ["bots"],
    queryFn: api.listBots,
    enabled: visible,
  });
  const suitesQuery = useQuery({
    queryKey: ["evaluation-suites", botId],
    queryFn: () => evaluationApi.suites(botId),
    enabled: visible && Boolean(botId),
  });
  const recordsQuery = useQuery({
    queryKey: ["evaluation-records", botId],
    queryFn: () => evaluationApi.list(botId ? { bot_id: botId } : {}),
    enabled: visible,
    refetchInterval: (query) => {
      const records = query.state.data as EvaluationRecord[] | undefined;
      return records?.some((record) => ACTIVE_STATUSES.has(record.status)) ? 2000 : false;
    },
  });

  const bots = botsQuery.data ?? [];
  const suites = suitesQuery.data ?? [];
  const records = recordsQuery.data ?? [];
  const latestSelected = records.find(
    (record) => record.evaluation_id === selectedEvaluation?.evaluation_id,
  ) ?? selectedEvaluation;
  const detailQuery = useQuery({
    queryKey: ["evaluation-detail", selectedEvaluation?.evaluation_id],
    queryFn: ({ signal }) => evaluationApi.get(selectedEvaluation!.evaluation_id, signal),
    enabled: visible && Boolean(selectedEvaluation),
    refetchInterval: query => ACTIVE_STATUSES.has(query.state.data?.status ?? latestSelected?.status ?? "error") ? 2000 : false,
  });
  const selectedRecord = detailQuery.data ?? latestSelected;
  const filteredRecords = records.filter(record => (recordTrack === "all" || trackForRecord(record)?.id === recordTrack)
    && (recordStatus === "all" || record.status === recordStatus));
  const suitesByTrack = useMemo(() => {
    const mapped = new Map<EvaluationTrack, EvaluationSuite>();
    for (const suite of suites) {
      if (suite.track === "agent" || suite.track === "qq_message_flow") {
        mapped.set(suite.track, suite);
      }
    }
    return mapped;
  }, [suites]);
  const activeForBot = records.find((record) => ACTIVE_STATUSES.has(record.status));

  useEffect(() => {
    if (!botId && bots.length) setBotId(bots[0].instance_id);
  }, [botId, bots]);

  useEffect(() => {
    for (const track of TRACKS) {
      const options = presetOptions(suitesByTrack.get(track.id) ?? null);
      if (!options.includes(presets[track.id])) {
        setPresets((current) => ({ ...current, [track.id]: options[0] }));
      }
    }
  }, [presets, suitesByTrack]);

  const startMutation = useMutation({
    mutationFn: async (track: EvaluationTrack) => {
      const suite = suitesByTrack.get(track);
      if (!suite) throw new Error("该测试轨道未安装或未进入目录。");
      return evaluationApi.create(buildSuiteRequest({
        botId,
        suiteId: suite.suite_id,
        caseIds: [],
        preset: presets[track],
        repetitions: 1,
        maxWallSeconds: 0,
        seed: 0,
        options: {},
        confirmExternalWrite: false,
        dryRun: false,
        llmJudge: false,
      }));
    },
    onMutate: (track) => {
      setSelectedTrack(track);
      setProblem(null);
    },
    onSuccess: async (record) => {
      Message.success(`已启动 ${trackForRecord(record)?.shortTitle ?? "评测"}`);
      setSelectedEvaluation(record);
      setTab("records");
      await queryClient.invalidateQueries({ queryKey: ["evaluation-records"] });
    },
    onError: (error) => {
      if (error instanceof EvaluationApiError) setProblem(error.problem);
      else setProblem({ code: "start_failed", message: formatApiError(error), checks: [] });
    },
  });

  const actionMutation = useMutation({
    mutationFn: async ({ action, id }: { action: "cancel" | "rerun" | "delete"; id: string }) => {
      if (action === "cancel") return evaluationApi.cancel(id);
      if (action === "rerun") return evaluationApi.rerun(id);
      return evaluationApi.remove(id);
    },
    onSuccess: async (_result, variables) => {
      Message.success(
        variables.action === "cancel"
          ? "已请求取消"
          : variables.action === "rerun"
            ? "已创建重跑"
            : "评测记录已删除",
      );
      if (variables.action === "delete") setSelectedEvaluation(null);
      if (variables.action === "rerun" && "evaluation_id" in _result) setSelectedEvaluation(_result);
      await queryClient.invalidateQueries({ queryKey: ["evaluation-records"] });
      await queryClient.invalidateQueries({ queryKey: ["evaluation-detail"] });
    },
    onError: (error) => Message.error(formatApiError(error)),
  });

  const refresh = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["bots"] }),
      queryClient.invalidateQueries({ queryKey: ["evaluation-suites"] }),
      queryClient.invalidateQueries({ queryKey: ["evaluation-records"] }),
      queryClient.invalidateQueries({ queryKey: ["evaluation-detail"] }),
    ]);
  };

  const columns: ColumnProps<EvaluationRecord>[] = [
    {
      title: "测试方向",
      width: 150,
      render: (_value, record) => {
        const track = trackForRecord(record);
        return track
          ? <Tag color={track.accent}>{track.shortTitle}</Tag>
          : <Tag color="gray">历史 / CLI</Tag>;
      },
    },
    {
      title: "状态",
      dataIndex: "status",
      width: 110,
      render: (value: string) => <Tag color={STATUS_COLORS[value] ?? "gray"}>{value}</Tag>,
    },
    {
      title: "通过情况",
      width: 200,
      render: (_value, record) => record.insights.counts ? (
        <Space size={4} wrap>
          <Text bold>{rateLabel(record.insights.pass_rate)}</Text>
          <Text type="secondary">{record.insights.counts.passed}/{record.insights.observed}</Text>
          {record.insights.counts.failed > 0 && <Tag color="red">失败 {record.insights.counts.failed}</Tag>}
          {record.insights.counts.error > 0 && <Tag color="orange">异常 {record.insights.counts.error}</Tag>}
          {!record.insights.complete && <Tag color="gray">部分</Tag>}
        </Space>
      ) : <Text type="secondary">未记录</Text>,
    },
    {
      title: "进度",
      width: 210,
      render: (_value, record) => (
        <Progress
          percent={record.progress.percent}
          size="small"
          status={record.status === "error" ? "error" : "normal"}
        />
      ),
    },
    {
      title: "创建时间",
      dataIndex: "created_at",
      width: 190,
      render: (value: string) => formatTime(value),
    },
    {
      title: "耗时",
      dataIndex: "duration_seconds",
      width: 110,
      render: (value: number | null) => formatDuration(value),
    },
    {
      title: "Git 版本",
      width: 230,
      render: (_value, record) => <span title={record.source_revision.commit ?? ""}>{revisionLabel(record)}</span>,
    },
    {
      title: "操作",
      width: 90,
      render: (_value, record) => (
        <Button type="text" size="small" onClick={() => setSelectedEvaluation(record)}>
          详情
        </Button>
      ),
    },
  ];

  if (!visible) return null;

  return (
    <PageSection
      title="测评中心"
      description="查看 Agent 评测与 QQ 链路测试的结果、版本记录和历史变化。"
      extra={tab !== "trends" ? <Button size="small" onClick={() => void refresh()}>刷新</Button> : undefined}
    >
      {tab !== "trends" && <div className="eval-history-filters eval-bot-selection">
        <Text bold>机器人</Text>
        <Select aria-label="评测机器人" value={botId || undefined} placeholder="选择机器人" loading={botsQuery.isLoading}
          options={bots.map(bot => ({ label: bot.display_name, value: bot.instance_id }))}
          onChange={value => { setBotId(String(value ?? "")); setSelectedEvaluation(null); setProblem(null); }} />
        <Text type="secondary">手动启动 · 每个测试点默认 1 次 · 同一机器人同时运行一条评测</Text>
      </div>}
      <Tabs activeTab={tab} onChange={setTab}>
        <Tabs.TabPane key="start" title="开始测试">
          <div className="eval-center-stack">
            {activeForBot && (
              <Alert
                type="warning"
                showIcon
                content={`该 Bot 正在运行 ${trackForRecord(activeForBot)?.shortTitle ?? "一条评测"}；完成或取消后才能启动下一条。`}
              />
            )}
            {problem && (
              <Alert
                type="error"
                showIcon
                title={`${problem.code}：${problem.message}`}
                content={
                  problem.checks.length ? (
                    <div className="eval-problem">
                      {problem.checks.filter((check) => !check.ok).map((check) => (
                        <Text key={check.code}>
                          {check.label}：{check.detail}{check.action ? `；${check.action}` : ""}
                        </Text>
                      ))}
                    </div>
                  ) : undefined
                }
              />
            )}

            <div className="eval-track-grid">
              {TRACKS.map((track) => {
                const suite = suitesByTrack.get(track.id) ?? null;
                const preset = presets[track.id];
                const options = presetOptions(suite);
                const selected = selectedTrack === track.id;
                const unavailable = !suite || !suite.ready;
                return (
                  <Card
                    key={track.id}
                    className={`eval-track-card${selected ? " eval-track-card-selected" : ""}`}
                    title={track.title}
                    extra={
                      suite?.ready
                        ? <Tag color="green">{suite.case_count} Cases</Tag>
                        : <Tag color="red">未就绪</Tag>
                    }
                    onClick={() => setSelectedTrack(track.id)}
                  >
                    <Paragraph>{track.description}</Paragraph>
                    <Descriptions
                      column={1}
                      size="small"
                      data={[
                        ...(track.framework ? [{ label: "测评框架", value: track.framework }] : []),
                        { label: "测试内容", value: track.includes },
                        { label: "明确不含", value: track.excludes },
                      ]}
                    />
                    <div className="eval-track-controls" onClick={(event) => event.stopPropagation()}>
                      <Text bold>范围</Text>
                      <Radio.Group
                        type="button"
                        value={preset}
                        onChange={(value) => setPresets((current) => ({
                          ...current,
                          [track.id]: value as SuitePreset,
                        }))}
                      >
                        {options.map((option) => (
                          <Radio key={option} value={option}>
                            {option === "quick" ? "快速" : option === "full" ? "完整" : "安全"}
                          </Radio>
                        ))}
                      </Radio.Group>
                      <Text type="secondary">
                        {unavailable
                          ? suite?.unavailable_reason || "测试轨道未安装。"
                          : presetDescription(suite, preset)}
                      </Text>
                      <Button
                        type="primary"
                        long
                        loading={startMutation.isPending && selectedTrack === track.id}
                        disabled={!botId || unavailable || Boolean(activeForBot)}
                        onClick={() => startMutation.mutate(track.id)}
                      >
                        {track.id === "agent"
                          ? preset === "full" ? "开始完整评测" : preset === "security" ? "开始安全评测" : "开始评测"
                          : `启动${track.shortTitle}${preset === "full" ? "完整测试" : "测试"}`}
                      </Button>
                    </div>
                  </Card>
                );
              })}
            </div>

            <Alert
              type="info"
              showIcon
              content="真实 QQ 登录、NapCat 在线和真实外部用户往返不在这两条本地 Evaluation 中；它们继续由基础设施诊断单独报告。"
            />
          </div>
        </Tabs.TabPane>

        <Tabs.TabPane key="records" title="运行记录">
          <Card className="eval-create-card">
            <div className="eval-history-filters">
              <Select aria-label="记录测试方向" value={recordTrack} onChange={setRecordTrack} options={[
                { value: "all", label: "全部方向" }, ...TRACKS.map(track => ({ value: track.id, label: track.shortTitle })),
              ]} />
              <Select aria-label="记录状态" value={recordStatus} onChange={setRecordStatus} options={[
                { value: "all", label: "全部状态" }, ...Object.keys(STATUS_COLORS).map(value => ({ value, label: value })),
              ]} />
              <Text type="secondary">{filteredRecords.length} 条评测</Text>
            </div>
            {recordsQuery.isLoading ? (
              <Spin style={{ display: "block", margin: "40px auto" }} />
            ) : recordsQuery.isError ? (
              <Alert type="error" content={formatApiError(recordsQuery.error)} />
            ) : filteredRecords.length ? (
              <Table
                rowKey="evaluation_id"
                columns={columns}
                data={filteredRecords}
                pagination={{ pageSize: 12 }}
                scroll={{ x: 1350 }}
              />
            ) : (
              <Empty description="还没有评测记录" />
            )}
          </Card>
        </Tabs.TabPane>
        <Tabs.TabPane key="trends" title="进步趋势">
          <Card className="eval-create-card">
            <EvaluationTrends initialBot={botId} bots={bots.map(bot => bot.instance_id)} visible={visible && tab === "trends"} onOpen={setSelectedEvaluation} />
          </Card>
        </Tabs.TabPane>
      </Tabs>

      <Drawer
        width="min(1080px, 100vw)"
        unmountOnExit
        escToExit
        autoFocus={false}
        afterOpen={() => detailTop.current?.focus({ preventScroll: true })}
        title="评测详情"
        visible={Boolean(selectedRecord)}
        onCancel={() => setSelectedEvaluation(null)}
        footer={null}
      >
        {selectedRecord && (
          <div ref={detailTop} tabIndex={-1} className="eval-detail-content">
          <Space direction="vertical" size={16} style={{ width: "100%" }}>
            <Space wrap><Tag color={STATUS_COLORS[selectedRecord.status] ?? "gray"}>{selectedRecord.status}</Tag>
              <Text>{selectedRecord.bot_id} · {modelLabel(selectedRecord)}</Text><Text>{dateLabel(selectedRecord.started_at || selectedRecord.created_at)}</Text>
              <Tag>{revisionLabel(selectedRecord)}</Tag></Space>
            <details className="eval-record-metadata"><summary>评测信息</summary>
            <Descriptions
              column={1}
              data={[
                {
                  label: "测试方向",
                  value: trackForRecord(selectedRecord)?.title ?? "历史 / CLI 评测",
                },
                { label: "Evaluation ID", value: selectedRecord.evaluation_id },
                { label: "Bot", value: selectedRecord.bot_id || "—" },
                {
                  label: "状态",
                  value: (
                    <Tag color={STATUS_COLORS[selectedRecord.status] ?? "gray"}>
                      {selectedRecord.status}
                    </Tag>
                  ),
                },
                {
                  label: "进度",
                  value: `${selectedRecord.progress.completed}/${selectedRecord.progress.total} (${selectedRecord.progress.percent}%)`,
                },
                { label: "创建", value: formatTime(selectedRecord.created_at) },
                { label: "完成", value: formatTime(selectedRecord.finished_at) },
                { label: "耗时", value: formatDuration(selectedRecord.duration_seconds) },
                { label: "创建时 Git", value: <span title={selectedRecord.source_revision.commit ?? ""}>{revisionLabel(selectedRecord)}</span> },
                { label: "版本采集时间", value: dateLabel(selectedRecord.source_revision.captured_at) },
                { label: "模型", value: modelLabel(selectedRecord) },
                { label: "配置 / 实现指纹", value: <code className="eval-fingerprint">{selectedRecord.insights.configuration_fingerprint || "未记录"}</code> },
              ]}
            />
            </details>
            {selectedRecord.error && <Alert type="error" content={selectedRecord.error} />}
            {detailQuery.isLoading && <Spin tip="正在读取测试点结果…" />}
            {detailQuery.isError && <Alert type="error" content={`详情读取失败：${formatApiError(detailQuery.error)}`}
              action={<Button size="small" onClick={() => void detailQuery.refetch()}>重试</Button>} />}
            <EvaluationResults key={selectedRecord.evaluation_id} record={selectedRecord} />
            <Space wrap>
              {ACTIVE_STATUSES.has(selectedRecord.status) && (
                <Button
                  status="warning"
                  loading={actionMutation.isPending}
                  onClick={() => actionMutation.mutate({
                    action: "cancel",
                    id: selectedRecord.evaluation_id,
                  })}
                >
                  取消
                </Button>
              )}
              {!ACTIVE_STATUSES.has(selectedRecord.status) && (
                <Button
                  loading={actionMutation.isPending}
                  onClick={() => actionMutation.mutate({
                    action: "rerun",
                    id: selectedRecord.evaluation_id,
                  })}
                >
                  重跑
                </Button>
              )}
              <Button href={evaluationExportUrl(selectedRecord.evaluation_id, "json")}>
                导出 JSON
              </Button>
              <Button href={evaluationExportUrl(selectedRecord.evaluation_id, "markdown")}>
                导出 Markdown
              </Button>
              {!ACTIVE_STATUSES.has(selectedRecord.status) && (
                <Button
                  status="danger"
                  onClick={() => Modal.confirm({
                    title: "删除评测记录",
                    content: "将删除该 Evaluation 的本地 artifact。此操作不可撤销。",
                    onOk: () => actionMutation.mutateAsync({
                      action: "delete",
                      id: selectedRecord.evaluation_id,
                    }),
                  })}
                >
                  删除
                </Button>
              )}
            </Space>
          </Space>
          </div>
        )}
      </Drawer>
    </PageSection>
  );
}
