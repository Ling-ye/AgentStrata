export interface ScheduleSettings {
  name: string; instruction: string; group_id: string; timezone: string; time: string;
  weekdays: number[]; enabled: boolean; timeout_seconds: number;
}
export interface ScheduleTask {
  id: string; revision: number; settings: ScheduleSettings; next_run: number | null;
  created_at: number; updated_at: number; last_run?: ScheduleRun | null; active_run?: ScheduleRun | null;
}
export interface ScheduleRun {
  id: string; task_id: string; revision: number; settings: ScheduleSettings;
  status: string; preview: boolean; trigger: string; scheduled_for: number; created_at: number;
  started_at: number | null; generated_at: number | null; delivery_started_at: number | null;
  finished_at: number | null; cancel_requested: boolean; gateway_run_id: string; error_code: string;
  window_start: string; window_end: string; prompt?: string; result_text?: string;
  receipts?: Array<{ stage: string; observed_at: number; provider_message_id?: string; error_code?: string }>;
}
export interface ScheduleOverview {
  tasks: ScheduleTask[];
  host: { available: boolean; state?: string; heartbeat_at?: number; error_code?: string };
}
export const weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];
export const activeRun = (run?: ScheduleRun) => !!run && ["queued", "researching", "delivering"].includes(run.status);
export const runLabels: Record<string, string> = {
  queued: "等待执行", researching: "调查与总结中", delivering: "正在投递", delivered: "平台已确认",
  previewed: "预览完成", failed: "失败", delivery_unknown: "投递结果未知", interrupted: "执行中断",
  cancelled: "已停止", skipped: "已跳过",
};
export const errorLabels: Record<string, string> = {
  previous_run_active: "上一轮尚未结束，本期已跳过。", configuration_changed: "配置已修改或暂停，旧运行已停止。",
  host_interrupted: "机器人执行中断；此轮不会自动重发。", timeout: "调查超过配置的执行时限。",
  research_failed: "AI 未生成有效报告，请检查模型、搜索配置或运行轨迹。",
  session_run_active: "目标群正在处理另一条消息，本轮未执行；可稍后手动运行。",
  superseded_schedule: "暂停连接期间已错过本期，系统只处理最近一期。",
  cancelled: "已请求停止；已经提交给平台的消息无法撤回。",
};
export function defaultSettings(): ScheduleSettings {
  return { name: "", instruction: "", group_id: "", timezone: "Asia/Shanghai", time: "09:00",
    weekdays: [0, 1, 2, 3, 4, 5, 6], enabled: false, timeout_seconds: 600 };
}
export function scheduleLabel(settings: ScheduleSettings): string {
  const days = settings.weekdays.length === 7 ? "每天" : settings.weekdays.map(day => weekdays[day]).join("、");
  return `${days} ${settings.time} · ${settings.timezone}`;
}
export function settingsError(settings: ScheduleSettings): string {
  if (!settings.name.trim() || !settings.instruction.trim()) return "请填写任务名称和调查要求";
  if (!/^[1-9][0-9]{4,19}$/.test(settings.group_id)) return "请输入有效的 QQ 群号";
  if (!/^(?:[01][0-9]|2[0-3]):[0-5][0-9]$/.test(settings.time)) return "执行时间格式为 HH:mm";
  if (!settings.weekdays.length) return "至少选择一天";
  try { new Intl.DateTimeFormat("zh-CN", { timeZone: settings.timezone }); }
  catch { return "请输入有效时区，例如 Asia/Shanghai"; }
  if (!Number.isInteger(settings.timeout_seconds) || settings.timeout_seconds < 30 || settings.timeout_seconds > 3600) return "执行时限为 30 至 3600 秒";
  return "";
}
export function displayTime(value: number | null | undefined, timezone = "Asia/Shanghai"): string {
  if (value == null) return "—";
  return new Date(value * 1000).toLocaleString("zh-CN", { timeZone: timezone, hour12: false });
}
export const tweetTemplate = "调查指定 X/Twitter 账号昨天发布的推文，逐条列出发布时间、中文要点和原文链接，区分原创、回复和转推；最后总结主要观点。统计按当前任务时区。只有拿到完整时间线时才报告总数，否则说明已检索到多少条及覆盖限制。\n账号或主页链接：请替换为 tibo 的准确 @用户名或主页 URL。";
