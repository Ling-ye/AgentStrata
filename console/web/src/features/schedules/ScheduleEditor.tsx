import { useContext, useEffect, useRef, useState } from "react";
import { Alert, Button, Checkbox, Form, Input, InputNumber, Modal, Space, Switch } from "@arco-design/web-react";
import { NavigationGuardContext } from "../../shared/navigationGuard";
import { defaultSettings, settingsError, tweetTemplate, weekdays, type ScheduleSettings, type ScheduleTask } from "./model";

export default function ScheduleEditor({ task, busy, error, onSave, onClose }: {
  task: ScheduleTask | null; busy: boolean; error: string;
  onSave: (settings: ScheduleSettings, requestId: string) => void; onClose: () => void;
}) {
  const [settings, setSettings] = useState<ScheduleSettings>(() => task ? { ...task.settings, weekdays: [...task.settings.weekdays] } : defaultSettings());
  const initial = useRef(JSON.stringify(settings));
  const requestId = useRef(crypto.randomUUID());
  const dirty = initial.current !== JSON.stringify(settings);
  const register = useContext(NavigationGuardContext);
  const discard = () => !dirty || window.confirm("放弃尚未保存的定时任务配置？");
  useEffect(() => register({ shouldBlock: () => dirty, confirm: async () => discard() }), [register, dirty]);
  useEffect(() => {
    const before = (event: BeforeUnloadEvent) => { if (dirty) { event.preventDefault(); event.returnValue = ""; } };
    window.addEventListener("beforeunload", before);
    return () => window.removeEventListener("beforeunload", before);
  }, [dirty]);
  function change<K extends keyof ScheduleSettings>(key: K, value: ScheduleSettings[K]) {
    requestId.current = crypto.randomUUID();
    setSettings(previous => ({ ...previous, [key]: value }));
  }
  const invalid = settingsError(settings);
  return <Modal visible title={task ? "编辑定时任务" : "新建定时任务"} className="schedule-editor" style={{ width: "min(680px, calc(100vw - 24px))" }}
    maskClosable={false} escToExit={false} onCancel={() => { if (!busy && discard()) onClose(); }}
    footer={<Space><Button disabled={busy} onClick={() => { if (discard()) onClose(); }}>取消</Button>
      <Button type="primary" loading={busy} disabled={!!invalid} onClick={() => onSave(settings, requestId.current)}>保存任务</Button></Space>}>
    <Form layout="vertical" disabled={busy}>
      <Form.Item label="任务名称" required><Input aria-label="任务名称" maxLength={120} value={settings.name} onChange={value => change("name", value)} placeholder="例如：tibo 昨日推文简报" /></Form.Item>
      <Form.Item label="让 AI 调查什么、怎样总结" required>
        <Input.TextArea aria-label="调查要求" value={settings.instruction} maxLength={16000} autoSize={{ minRows: 5, maxRows: 12 }} onChange={value => change("instruction", value)} placeholder="写清调查对象、来源、时间范围、总结要求。" />
        <Button type="text" size="small" onClick={() => change("instruction", tweetTemplate)}>填入昨日推文示例</Button>
      </Form.Item>
      <div className="schedule-form-grid">
        <Form.Item label="目标 QQ 群号" required><Input aria-label="目标 QQ 群号" value={settings.group_id} onChange={value => change("group_id", value.trim())} placeholder="机器人已加入的群" /></Form.Item>
        <Form.Item label="时区" required><Input aria-label="时区" value={settings.timezone} onChange={value => change("timezone", value.trim())} /></Form.Item>
        <Form.Item label="当地执行时间" required><Input aria-label="当地执行时间" type="time" value={settings.time} onChange={value => change("time", value)} /></Form.Item>
        <Form.Item label="每次执行时限（秒）"><InputNumber aria-label="执行时限" min={30} max={3600} precision={0} value={settings.timeout_seconds} onChange={value => change("timeout_seconds", value)} /></Form.Item>
      </div>
      <Form.Item label="执行日期" required><Checkbox.Group value={settings.weekdays.map(String)} options={weekdays.map((label, index) => ({ label, value: String(index) }))}
        onChange={values => change("weekdays", values.map(Number).sort())} /></Form.Item>
      <Form.Item label="自动执行"><Switch checked={settings.enabled} onChange={value => change("enabled", value)} /> <span>{settings.enabled ? "到点自动调查并推送" : "暂停；仍可手动预览"}</span></Form.Item>
      <Alert type="info" content="使用此机器人的模型和公开研究工具。‘昨天’按任务时区固定；搜索不完整会说明覆盖限制。机器人需要持续运行。" />
      {(error || invalid) && <Alert style={{ marginTop: 12 }} type={error ? "error" : "warning"} content={error || invalid} />}
    </Form>
  </Modal>;
}
