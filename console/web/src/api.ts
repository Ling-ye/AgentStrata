import type {
  BotInstance,
  BotInventory,
  BotStatus,
  BotTaskDetail,
  ContextSnapshot,
  BotToolConfig,
  CatalogItem,
  InfraService,
  JobsResponse,
  Overview,
  ProvisionEnvPayload,
  ProvisionEnvResult,
  ProvisionSchema,
  Task,
  TaskEventsResponse,
  TaskFlowResponse,
  TasksResponse,
  ToolUpdateResult,
  XhsLoginQrcode,
  XhsLoginStatus,
} from "./types";

async function req<T>(url: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(url, init);
  if (!resp.ok) {
    let detail = `${resp.status}`;
    try {
      const body = await resp.json();
      detail = typeof body.detail === "string"
        ? body.detail
        : JSON.stringify(body.detail ?? body);
    } catch {
      /* ignore */
    }
    throw new Error(detail);
  }
  return (await resp.json()) as T;
}

async function fireAndForgetReq<T>(url: string, init?: RequestInit, fallback?: T): Promise<T> {
  try {
    return await req<T>(url, init);
  } catch (e) {
    if (e instanceof TypeError && fallback !== undefined) {
      return fallback;
    }
    throw e;
  }
}

export const api = {
  overview: () => req<Overview>("/api/overview"),
  listBots: () => req<BotInstance[]>("/api/bots"),
  status: (id: string) => req<BotStatus>(`/api/bots/${id}/status`),
  inventory: (id: string) => req<BotInventory>(`/api/bots/${id}/inventory`),
  jobs: (id: string) => req<JobsResponse>(`/api/bots/${id}/jobs`),
  tasks: (id: string) => req<TasksResponse>(`/api/bots/${id}/tasks?limit=50`),
  taskDetail: (id: string, taskId: string) =>
    req<BotTaskDetail>(`/api/bots/${id}/tasks/${encodeURIComponent(taskId)}`),
  taskEvents: (id: string, taskId: string, limit = 500) =>
    req<TaskEventsResponse>(
      `/api/bots/${id}/tasks/${encodeURIComponent(taskId)}/events?limit=${limit}`,
    ),
  taskFlow: (id: string, taskId: string) =>
    req<TaskFlowResponse>(
      `/api/bots/${id}/tasks/${encodeURIComponent(taskId)}/flow`,
    ),
  deleteTask: (id: string, taskId: string) =>
    req<{ ok: boolean; deleted: boolean; task_id: string; status: string }>(
      `/api/bots/${id}/tasks/${encodeURIComponent(taskId)}`,
      { method: "DELETE" },
    ),
  taskContext: (id: string, taskId: string, snapshotId: string) =>
    req<ContextSnapshot>(
      `/api/bots/${id}/tasks/${encodeURIComponent(taskId)}/contexts/${encodeURIComponent(snapshotId)}`,
    ),

  control: (id: string, verb: "start" | "stop" | "restart") =>
    req<unknown>(`/api/bots/${id}/${verb}`, { method: "POST" }),

  sync: (id: string, restart = true) =>
    req<Task>(`/api/bots/${id}/sync?restart=${restart}`, { method: "POST" }),
  rebuild: (id: string, restart = true) =>
    req<Task>(`/api/bots/${id}/rebuild?restart=${restart}`, { method: "POST" }),
  update: (id: string) => req<Task>(`/api/bots/${id}/update`, { method: "POST" }),
  dump: (id: string) => req<Task>(`/api/bots/${id}/dump`, { method: "POST" }),

  // 首次部署
  provisionSchema: (id: string) => req<ProvisionSchema>(`/api/bots/${id}/provision/schema`),
  provisionEnv: (id: string, payload: ProvisionEnvPayload) =>
    req<ProvisionEnvResult>(
      `/api/bots/${id}/provision/env`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      },
    ),
  register: (id: string) => req<Task>(`/api/bots/${id}/register`, { method: "POST" }),
  setupAction: (id: string, actionId: string, verb = "start") =>
    req<Task>(`/api/bots/${id}/setup-actions/${actionId}?verb=${verb}`, { method: "POST" }),

  sharedServiceXhsStart: () =>
    req<{ ok: boolean; stdout?: string; stderr?: string }>("/api/shared-services/xhs/start", {
      method: "POST",
    }),
  xhsLoginQrcode: () =>
    req<XhsLoginQrcode>("/api/shared-services/xhs/login-qrcode", { method: "POST" }),
  xhsCheckLogin: () =>
    req<XhsLoginStatus>("/api/shared-services/xhs/check-login", { method: "POST" }),

  // 更新控制台自身（重建前端 + 重启后端）。fire-and-forget：调用后服务短暂不可用。
  updateConsole: () =>
    fireAndForgetReq<{ ok: boolean; message?: string }>(
      "/api/console/update",
      { method: "POST" },
      { ok: true, message: "控制台更新已触发，服务正在重启，稍后将自动刷新。" },
    ),

  // Tool catalog & bot tool configuration
  catalog: () => req<CatalogItem[]>("/api/catalog"),
  catalogItem: (itemId: string) => req<CatalogItem>(`/api/catalog/${itemId}`),
  botTools: (id: string) => req<BotToolConfig>(`/api/bots/${id}/tools`),
  updateBotTools: (id: string, config: BotToolConfig, opts?: { apply?: boolean }) =>
    req<ToolUpdateResult | Task>(`/api/bots/${id}/tools?apply=${opts?.apply ? "true" : "false"}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(config),
    }),

  task: (taskId: string) => req<Task>(`/api/tasks/${taskId}`),

  // 基础设施服务
  infraServices: () => req<InfraService[]>("/api/infra"),
  infraComposeUp: () =>
    req<{ ok: boolean; stdout?: string; stderr?: string }>("/api/infra/compose-up", { method: "POST" }),
  infraAction: (id: string, verb: string) =>
    req<{ ok: boolean } | Task>(`/api/infra/${id}/${verb}`, { method: "POST" }),
  infraLoginQrcode: (id: string) =>
    req<XhsLoginQrcode>(`/api/infra/${id}/login/qrcode`, { method: "POST" }),
  infraLoginCheck: (id: string) =>
    req<XhsLoginStatus>(`/api/infra/${id}/login/check`, { method: "POST" }),
  infraLoginToken: (id: string) =>
    req<{ ok: boolean; token: string }>(`/api/infra/${id}/login/token`, {
      method: "POST",
      cache: "no-store",
    }),
};

/** 通过 SSE 跟读任务输出。返回 close 函数。 */
export function streamTask(
  taskId: string,
  onLine: (line: string) => void,
  onEnd: () => void,
  onStatus?: (status: "connecting" | "live" | "reconnecting") => void,
): () => void {
  const es = new EventSource(`/api/tasks/${taskId}/stream`);
  onStatus?.("connecting");
  es.onopen = () => onStatus?.("live");
  es.onmessage = (e) => {
    try {
      const data = JSON.parse(e.data);
      if (typeof data.line === "string") onLine(data.line);
    } catch {
      /* keepalive comments arrive as comments, not messages */
    }
  };
  es.addEventListener("end", () => {
    es.close();
    onEnd();
  });
  // EventSource 会自动重连；连接中断不代表后台任务已经结束。
  es.onerror = () => onStatus?.("reconnecting");
  return () => es.close();
}

/** 通过 SSE 跟读实例日志。返回 close 函数。 */
export function streamLogs(
  id: string,
  source: string,
  onLine: (line: string) => void,
  onStatus?: (status: "connecting" | "live" | "reconnecting") => void,
): () => void {
  const es = new EventSource(`/api/bots/${id}/logs/stream?source=${source}`);
  onStatus?.("connecting");
  es.onopen = () => onStatus?.("live");
  es.onmessage = (e) => {
    try {
      const data = JSON.parse(e.data);
      if (typeof data.line === "string") onLine(data.line);
    } catch {
      /* ignore */
    }
  };
  // EventSource 在连接中断时会自动重连，onerror 期间标记为重连中。
  es.onerror = () => onStatus?.("reconnecting");
  return () => es.close();
}

/** 通过 SSE 跟读基础设施服务日志。返回 close 函数。 */
export function streamInfraLogs(
  serviceId: string,
  onLine: (line: string) => void,
  onStatus?: (status: "connecting" | "live" | "reconnecting") => void,
): () => void {
  const es = new EventSource(`/api/infra/${serviceId}/logs/stream`);
  onStatus?.("connecting");
  es.onopen = () => onStatus?.("live");
  es.onmessage = (e) => {
    try {
      const data = JSON.parse(e.data);
      if (typeof data.line === "string") onLine(data.line);
    } catch {
      /* ignore */
    }
  };
  es.onerror = () => onStatus?.("reconnecting");
  return () => es.close();
}

/** 通过 SSE 跟读控制台后端服务日志。返回 close 函数。 */
export function streamConsoleLogs(
  onLine: (line: string) => void,
  onStatus?: (status: "connecting" | "live" | "reconnecting") => void,
): () => void {
  const es = new EventSource("/api/console/logs/stream");
  onStatus?.("connecting");
  es.onopen = () => onStatus?.("live");
  es.onmessage = (e) => {
    try {
      const data = JSON.parse(e.data);
      if (typeof data.line === "string") onLine(data.line);
    } catch {
      /* ignore */
    }
  };
  es.onerror = () => onStatus?.("reconnecting");
  return () => es.close();
}
