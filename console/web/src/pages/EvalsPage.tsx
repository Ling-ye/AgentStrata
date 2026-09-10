import { useEffect, useRef, useState } from "react";
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
  formatApiError,
  type EvaluationRecord,
  type EvaluationStatus,
} from "../features/evals/model";
import type { ColumnProps } from "../shared/ui/arcoTypes";
import PageSection from "../shared/ui/PageSection";
import BenchmarkWorkbench from "../features/evals/BenchmarkWorkbench";
import { asObject } from "../features/evals/trialModel";
import { BenchmarkSnapshot } from "../features/evals/BenchmarkSnapshot";
import EvaluationTrends from "../features/evals/EvaluationTrends";
import { EvaluationResults } from "../features/evals/EvaluationResults";
import { dateLabel, evaluationSuiteId, modelLabel, rateLabel, revisionLabel } from "../features/evals/insightsModel";

const { Text } = Typography;
const hasLlmPrimary = (record: EvaluationRecord) => asObject(asObject(record.benchmark).scoring).primary === "llm_judge";

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
    title: "Agent / 模型能力",
    shortTitle: "Agent / 模型能力",
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
  return TRACKS.find((track) => track.suiteId === id) ?? (id ? TRACKS[0] : null);
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

export default function EvalsPage({ visible = true }: Props) {
  const queryClient = useQueryClient();
  const detailTop = useRef<HTMLDivElement>(null);
  const [tab, setTab] = useState("start");
  const [botId, setBotId] = useState("");
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
  const activeForBot = records.find((record) => ACTIVE_STATUSES.has(record.status));

  useEffect(() => {
    if (!botId && bots.length) setBotId(bots[0].instance_id);
  }, [botId, bots]);

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
      title: "测评集 / 框架", width: 220, render: (_value, record) => <BenchmarkSnapshot record={record} compact />,
    },
    {
      title: "通过情况",
      width: 200,
      render: (_value, record) => record.insights.counts ? (
        <Space size={4} wrap>
          <Text bold>{rateLabel(hasLlmPrimary(record) ? record.insights.quality.score : record.insights.pass_rate)}</Text>
          <Text type="secondary">{hasLlmPrimary(record) ? "LLM " : ""}{record.insights.counts.passed}/{hasLlmPrimary(record) ? record.insights.quality.scored : record.insights.observed}</Text>
          {record.insights.counts.failed > 0 && <Tag color="red">失败 {record.insights.counts.failed}</Tag>}
          {record.insights.counts.error > 0 && <Tag color="orange">异常 {record.insights.counts.error}</Tag>}
          {!record.insights.complete && <Tag color="gray">部分</Tag>}
        </Space>
      ) : <Text type="secondary">未记录</Text>,
    },
    {
      title: "LLM 评价", width: 150, render: (_value, record) => <Space direction="vertical" size={2}>
        <Text>{hasLlmPrimary(record) ? "通过 / 不通过判定" : record.insights.quality.score === null ? "—" : record.insights.quality.score.toFixed(2)}</Text>
        <Text type="secondary">已评分 {record.insights.quality.scored} / {record.insights.quality.expected}</Text>
      </Space>,
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
      description="按测评集选择题目与评分方法，查看实际结果和可比趋势。"
      extra={tab !== "trends" ? <Button size="small" onClick={() => void refresh()}>刷新</Button> : undefined}
    >
      {tab !== "trends" && <div className="eval-history-filters eval-bot-selection">
        <Text bold>机器人</Text>
        <Select aria-label="评测机器人" value={botId || undefined} placeholder="选择机器人" loading={botsQuery.isLoading}
          options={bots.map(bot => ({ label: bot.display_name, value: bot.instance_id }))}
          onChange={value => { setBotId(String(value ?? "")); setSelectedEvaluation(null); }} />
        <Text type="secondary">手动启动 · 每个测试点默认 1 次 · 同一机器人同时运行一条评测</Text>
      </div>}
      <Tabs activeTab={tab} onChange={setTab}>
        <Tabs.TabPane key="start" title="开始测试">
          {suitesQuery.isLoading ? <Spin /> : suitesQuery.isError ? <Alert type="error" content={formatApiError(suitesQuery.error)} /> :
            <BenchmarkWorkbench key={botId} botId={botId} suites={suites} active={Boolean(activeForBot)} onCreated={record => {
              setSelectedEvaluation(record); setTab("records");
              void queryClient.invalidateQueries({ queryKey: ["evaluation-records"] });
            }} />}

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
            <EvaluationTrends suites={suites} initialBot={botId} bots={bots.map(bot => bot.instance_id)} visible={visible && tab === "trends"} onOpen={setSelectedEvaluation} />
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
            <BenchmarkSnapshot record={selectedRecord} />
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
