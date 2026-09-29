import type { ScheduleOverview, ScheduleRun, ScheduleSettings, ScheduleTask } from "./model";

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, { ...init, headers: { "Content-Type": "application/json", ...init?.headers } });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    const detail = body.detail;
    throw new Error(typeof detail === "string" ? detail : detail?.message ?? `请求失败（${response.status}）`);
  }
  return response.json();
}
const base = (bot: string) => `/api/bots/${encodeURIComponent(bot)}`;
export const schedulesApi = {
  overview: (bot: string, signal?: AbortSignal) => request<ScheduleOverview>(`${base(bot)}/schedules`, { signal }),
  create: (bot: string, settings: ScheduleSettings, request_id: string) => request<ScheduleTask>(`${base(bot)}/schedules`,
    { method: "POST", body: JSON.stringify({ settings, request_id }) }),
  update: (bot: string, task: ScheduleTask, settings: ScheduleSettings) => request<ScheduleTask>(`${base(bot)}/schedules/${task.id}`,
    { method: "PUT", body: JSON.stringify({ settings, revision: task.revision }) }),
  remove: (bot: string, task: ScheduleTask) => request(`${base(bot)}/schedules/${task.id}?revision=${task.revision}`, { method: "DELETE" }),
  start: (bot: string, task: ScheduleTask, preview: boolean, request_id: string) => request<ScheduleRun>(`${base(bot)}/schedules/${task.id}/runs`,
    { method: "POST", body: JSON.stringify({ revision: task.revision, preview, request_id }) }),
  history: (bot: string, task: string, offset: number, signal?: AbortSignal) => request<{ runs: ScheduleRun[]; next_offset: number | null }>(
    `${base(bot)}/schedule-runs?offset=${offset}${task ? `&task_id=${encodeURIComponent(task)}` : ""}`, { signal }),
  detail: (bot: string, run: string, signal?: AbortSignal) => request<ScheduleRun>(`${base(bot)}/schedule-runs/${encodeURIComponent(run)}`, { signal }),
  cancel: (bot: string, run: string) => request<ScheduleRun>(`${base(bot)}/schedule-runs/${encodeURIComponent(run)}/cancel`, { method: "POST" }),
};
