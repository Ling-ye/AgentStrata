export interface NapcatWebuiToken {
  ok: boolean;
  token: string;
  url: string;
  container: string;
  running: boolean;
  bootstrapped?: boolean;
}

export interface InfraService {
  id: string;
  display_name: string;
  service_type: "compose" | "standalone";
  state: "healthy" | "running" | "stopped" | "unhealthy" | "not_found";
  color: "green" | "yellow" | "red" | "grey";
  container: string | null;
  uptime_s: number | null;
  actions: string[];
  has_login: boolean;
  has_doctor: boolean;
  instance_id: string | null;
  login_state: "logged_in" | "logged_out" | null;
  login_type: "qrcode" | "webui_link" | null;
  env_configured?: boolean;
  checks?: HealthCheck[];
  reasons?: string[];
  extra: Record<string, unknown>;
}

export interface ToolPackGroup {
  namespace: string;
  label: string;
  tool_packs: string[];
}

export interface BotInstance {
  instance_id: string;
  display_name: string;
  platform: string;
  wsl_home: string;
  workspace_root: string;
  log_dir: string;
  env_file: string;
  cc_home: string;
  project_name: string;
  unit: string;
  is_deployed: boolean;
}

export interface McpServiceStatus {
  id: string;
  container: string;
  running: boolean;
  health: string;
  color: "green" | "yellow" | "red" | "grey";
}

export interface BotEnabledService {
  id: string;
  service_id: string;
  display_name: string;
  service_type: "compose" | "standalone" | "embedded" | "remote";
  state: string;
  color: "green" | "yellow" | "red" | "grey";
  container: string | null;
  uptime_s: number | null;
  reasons: string[];
  actions: string[];
  has_login: boolean;
  has_doctor: boolean;
  instance_id: string | null;
}

export interface BotStatus {
  instance_id: string;
  display_name: string;
  platform: string;
  is_deployed: boolean;
  unit: string;
  systemd_available: boolean;
  unit_installed: boolean;
  registered: boolean;
  active_state: string;
  sub_state: string;
  enabled: string;
  pid: number | null;
  since: string | null;
  running: boolean;
  cc_log: string | null;
  cc_log_age_s: number | null;
  cc_log_size: number | null;
  ws_connected: boolean | null;
  error_count: number;
  questions_today: number | null;
  mcp_services?: McpServiceStatus[];
  enabled_services?: BotEnabledService[];
  tool_packs?: ToolPackGroup[];
  checks?: HealthCheck[];
  reasons?: string[];
}

export interface HealthCheck {
  name: string;
  ok: boolean;
  severity: "critical" | "warning" | "info";
  message: string;
}

export interface OverviewIssue {
  id: string;
  severity: "critical" | "warning" | "info";
  source_type: "bot" | "infra" | "task";
  source_id: string;
  source_name: string;
  title: string;
  detail: string;
  action_label: string;
  target_page: "overview" | "services" | "bots" | "settings";
  target_id: string;
  created_at: number;
}

export interface OverviewSummary {
  bots_total: number;
  bots_running: number;
  bots_unhealthy: number;
  infra_total: number;
  infra_healthy: number;
  infra_unhealthy: number;
  tasks_running: number;
  tasks_failed_recent: number;
  issues_critical: number;
  issues_warning: number;
}

export interface OverviewBotStatus extends BotStatus {
  health_color: "green" | "yellow" | "red" | "grey";
  health_label: string;
}

export interface Overview {
  generated_at: string;
  summary: OverviewSummary;
  issues: OverviewIssue[];
  bots: OverviewBotStatus[];
  infra: InfraService[];
  active_tasks: Task[];
  recent_failures: Array<Task | BotTask>;
}

// ---------------------------------------------------------------------------
// Bot Tool Inventory
// ---------------------------------------------------------------------------

export interface McpBinding {
  ref: string;
  title: string;
  enabled: boolean;
  risk: string;
  exposure: string;
  allowed_subagents: string[];
  transport: string;
  infra_service_id: string | null;
  infra_state: string;
  infra_color: "green" | "yellow" | "red" | "grey" | "";
}

export interface ToolPackDetail {
  id: string;
  namespace: string;
  label: string;
  description: string;
  has_tools?: boolean;
  has_prompts?: boolean;
}

export interface SubagentBudget {
  max_model_turns?: number;
  max_tool_calls?: number;
  timeout_seconds?: number;
  max_output_chars?: number;
}

export interface SubagentInfo {
  name: string;
  tool_name: string;
  kind: string;
  summary: string;
  workflow_tags: string[];
  budget: SubagentBudget;
}

export interface FileEntry {
  path: string;
  exists: boolean;
}

export interface BotConfig {
  persona: FileEntry | null;
  refusal: FileEntry | null;
  safety: FileEntry | null;
  roles: Record<string, FileEntry> | null;
  memory: { provider: string; namespace: string; schema: string } | null;
  rag: { sources: string } | null;
  codebases: { registry: string } | null;
  skills: { manifest: string } | null;
  access: {
    private_require_whitelist: boolean;
    group_require_whitelist: boolean;
    group_require_mention: boolean;
  } | null;
}

export interface BotInventory {
  instance_id: string;
  display_name: string;
  platform: string;
  mcp_services: McpBinding[];
  tool_packs: ToolPackDetail[];
  tool_features: ToolPackDetail[];
  hidden_tools: string[];
  agent_presets: SubagentInfo[];
  workflows: string[];
  config: BotConfig;
}

// ---------------------------------------------------------------------------
// Tool Catalog & Bot Tool Config
// ---------------------------------------------------------------------------

export interface ToolBrief {
  name: string;
  summary: string;
  category: string;
  weight: string;
  requires_role: string | null;
}

export interface CatalogItem {
  id: string;
  kind: "tool_pack" | "tool_feature" | "mcp" | "subagent" | "workflow" | "prompt" | "context_source";
  surface: "tools" | "prompts" | "agents" | "context";
  name: string;
  description: string;
  category: string;
  tags: string[];
  risk: string;
  has_tools: boolean;
  has_prompts: boolean;
  requires_env: string[];
  infra_service_id: string;
  tools: ToolBrief[];
}

export interface McpServerRef {
  ref: string;
  enabled: boolean;
}

export interface BotToolConfig {
  tools: {
    packs: string[];
    features: string[];
    hide: string[];
    mcp: { servers: McpServerRef[] };
  };
  agents: {
    presets: string[];
    workflows: string[];
  };
}

export interface ToolUpdateResult {
  ok: boolean;
  files_modified: string[];
  warnings: string[];
  restart_required: boolean;
  apply_required?: boolean;
}

export interface Task {
  id: string;
  instance_id: string;
  kind: string;
  status: "running" | "done" | "failed";
  exit_code: number | null;
  created_at: number;
  finished_at: number | null;
  lines: string[];
  line_count: number;
}

export interface ProvisionEnvPayload {
  [key: string]: string | undefined;
  feishu_app_id?: string;
  feishu_app_secret?: string;
  chat_api_key?: string;
  chat_base_url?: string;
  chat_model?: string;
  add_owner_ids?: string;
  tavily_api_key?: string;
  qq_account?: string;
  qq_ws_url?: string;
  qq_access_token?: string;
  qq_allow_from?: string;
  qq_webui_port?: string;
}

export interface ProvisionField {
  env_key: string;
  field: string;
  required: boolean;
  default?: string | null;
  description?: string;
}

export interface SetupAction {
  id: string;
  label: string;
  description?: string;
}

export interface SharedServiceStep {
  id: string;
  service: "xhs" | string;
  label: string;
  description?: string;
  required?: boolean;
}

export interface ProvisionSchema {
  platform: string;
  adapter_id: string;
  common_fields: ProvisionField[];
  fields: ProvisionField[];
  setup_actions: SetupAction[];
  shared_services?: SharedServiceStep[];
}

export interface XhsLoginQrcode {
  ok: boolean;
  image_data_url?: string;
  already_logged_in?: boolean;
  message?: string;
  expires_at?: number | null;
}

export interface XhsLoginStatus {
  ok: boolean;
  logged_in: boolean;
  message?: string;
}

export interface Job {
  job_id: string;
  user_dir: string;
  tool_name?: string;
  submitter?: string;
  status: string;
  message: string;
  stage?: string;
  error_code?: string;
  details?: Record<string, unknown>;
  submitted_at?: number | null;
  updated_at: number | null;
  started_at?: number | null;
  finished_at?: number | null;
  elapsed_s?: number | null;
  sort_time: number;
  progress_tail: string;
  progress_tail_integrity_gap: boolean;
  stdout_age_s: number | null;
  path: string;
}

export interface JobsResponse {
  instance_id: string;
  workspace_root: string;
  workspace_exists: boolean;
  count: number;
  integrity_gap: boolean;
  jobs: Job[];
}

export interface TaskTool {
  name: string;
  status: string;
  started_at?: number | null;
  finished_at?: number | null;
  elapsed_s?: number | null;
  summary?: string;
  error?: string | null;
}

export interface LlmUsageTotals {
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  reasoning_tokens: number;
  cached_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  llm_calls: number;
  cache_hit_calls: number;
  cache_hit_rate: number;
  cache_hit_call_rate: number;
}

export interface LlmCallUsage {
  model: string;
  iteration: number;
  finish_reason?: string;
  usage?: Partial<LlmUsageTotals>;
  trace_id?: string | null;
  span_id?: string | null;
  parent_span_id?: string | null;
  depth?: number;
  recorded_at?: number;
}

export interface TokenUsageV2 extends Partial<LlmUsageTotals> {
  input_tokens?: number;
  non_cached_input_tokens?: number;
  output_tokens?: number;
}

export interface TaskForecastV2 {
  status: "rough" | "ready" | "insufficient";
  model?: string;
  context_kind?: string;
  sample_count?: number;
  min_samples?: number;
  max_samples?: number;
  estimator_version?: string;
  baseline?: TokenUsageV2 | null;
  fixed_at?: number | null;
}

export interface TaskStepV2 {
  step_id: string;
  type: string;
  parent_step_id?: string | null;
  depth: number;
  status: string;
  title: string;
  started_at?: number | null;
  finished_at?: number | null;
  elapsed_s?: number | null;
  summary?: string;
  error?: string | null;
  metadata?: Record<string, unknown>;
  estimated_usage?: TokenUsageV2;
  actual_usage?: TokenUsageV2;
  inclusive_usage?: TokenUsageV2;
  raw_event_types?: string[];
}

export interface BotTaskWorkspace {
  root?: string;
  chat_kind?: string;
  chat_id?: string;
  user_id?: string;
  user_name?: string;
}

export interface BotTaskJobStatus {
  job_id: string;
  status: string;
  stage: string;
  message?: string;
  error_code?: string;
  details?: Record<string, unknown>;
}

export interface BotTaskJobResult {
  job_id: string;
  ok: boolean;
  status: string;
  stage?: string;
  error_code?: string;
  summary?: string;
  error?: string;
  outputs?: string[];
  finished_at?: number | null;
}

export interface ContextSnapshotSummary {
  snapshot_id: string;
  backend: string;
  model: string;
  iteration: number;
  coverage: "exact_model_input" | "adapter_visible" | "provider_opaque" | string;
  capture_status: string;
  redacted: boolean;
  truncated: boolean;
  captured_at: number | null;
  message_count: number;
  effective_message_count: number;
  tool_schema_count: number;
  resource_count: number;
  estimated_tokens: number;
  reasoning_effort: string;
  context_kind: string;
  omitted: string[];
  trace_id: string;
  span_id: string;
  parent_span_id: string;
  depth: number;
  role: "main" | "subagent";
}

export interface ContextSnapshot extends Partial<ContextSnapshotSummary> {
  schema_version?: number;
  snapshot_id: string;
  task_id?: string;
  session_messages?: Array<Record<string, unknown>>;
  effective_messages?: Array<Record<string, unknown>>;
  tool_schemas?: Array<Record<string, unknown>>;
  resources?: unknown[];
  model_selection?: Record<string, unknown>;
  sanitization?: Record<string, unknown>;
}

export interface BotTask {
  schema_version?: 2;
  task_id: string;
  description: string;
  progress: string;
  current_step?: string;
  status: string;
  submitter: string;
  asked_at: number | null;
  started_at?: number | null;
  finished_at?: number | null;
  elapsed_s?: number | null;
  updated_at: number | null;
  sort_time: number;
  tools?: TaskTool[];
  usage_totals?: Partial<LlmUsageTotals>;
  llm_calls?: LlmCallUsage[];
  forecast?: TaskForecastV2;
  primary_model?: string;
  context_kind?: string;
  context_snapshots?: ContextSnapshotSummary[];
  activity_summary?: {
    provider_total: number;
    provider_retained: number;
    provider_dropped: number;
    truncated: boolean;
  };
  summary_limits?: {
    tools_total: number;
    tools_retained: number;
    steps_total: number;
    steps_retained: number;
    llm_calls_total: number;
    llm_calls_retained: number;
    llm_calls_truncated: boolean;
    context_snapshots_total: number;
    context_snapshots_retained: number;
    context_snapshots_truncated: boolean;
    context_snapshots_minimal: boolean;
    input_resources_total: number;
    input_resources_retained: number;
    input_resources_truncated: boolean;
    payload_truncated: boolean;
    truncated: boolean;
  };
  job_ids: string[];
  job_statuses?: BotTaskJobStatus[];
  job_results?: BotTaskJobResult[];
  current_job_stage?: string;
  current_job_error_code?: string;
  session_id?: string;
  message_id?: string;
  workspace?: BotTaskWorkspace;
  path?: string;
}

export interface BotTaskDetail extends BotTask {
  steps: TaskStepV2[];
  timing: {
    model_s: number;
    activity_s: number;
    tool_s: number;
    background_s: number;
    routing_s: number;
  };
  actual_usage: TokenUsageV2;
  actual_cost?: {
    status: "estimated" | "partial" | "unpriced";
    estimated_rmb: number;
    priced_calls: number;
    unpriced_models: string[];
    note: string;
  };
}

export interface TaskRawEvent {
  event: string;
  recorded_at?: number;
  data?: Record<string, unknown>;
  source?: string;
  job_id?: string;
}

export interface TaskEventsResponse {
  task_id: string;
  count: number;
  limit: number;
  truncated: boolean;
  integrity_gap: boolean;
  events: TaskRawEvent[];
}

export interface TasksResponse {
  instance_id: string;
  workspace_root: string;
  workspace_exists: boolean;
  count: number;
  tasks: BotTask[];
}
