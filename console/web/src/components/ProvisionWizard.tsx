import { useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  Button,
  Form,
  Input,
  Message,
  Modal,
  Space,
  Spin,
  Steps,
  Typography,
} from "@arco-design/web-react";
import LogConsole from "./LogConsole";
import { api, streamTask } from "../api";
import {
  editableProvisionFields,
  isTerminalSetupAction,
  setupActionVerb,
  terminalQuickstartCommand,
} from "../provisioning";
import type {
  BotInstance,
  ProvisionEnvPayload,
  ProvisionField,
  ProvisionSchema,
  SharedServiceStep,
  SetupAction,
  Task,
} from "../types";

const { Text } = Typography;

interface Props {
  bot: BotInstance;
  onClose: () => void;
  onChanged: () => void;
}

type StepState = "wait" | "process" | "finish" | "error";

function renderField(field: ProvisionField) {
  const placeholder = field.configured
    ? "已配置；留空将保留现有值"
    : field.description || field.default || "";
  const control = field.secret
    ? <Input.Password placeholder={placeholder} />
    : <Input placeholder={placeholder} />;

  return (
    <Form.Item
      key={field.env_key}
      field={field.field}
      label={field.label || field.env_key}
      initialValue={field.configured ? undefined : field.default || undefined}
      rules={field.required && !field.configured ? [{ required: true, message: "必填" }] : []}
    >
      {control}
    </Form.Item>
  );
}

export default function ProvisionWizard({ bot, onClose, onChanged }: Props) {
  const [schema, setSchema] = useState<ProvisionSchema | null>(null);
  const [schemaError, setSchemaError] = useState("");
  const setupActions = useMemo(
    () => (schema?.setup_actions?.length ? schema.setup_actions : []),
    [schema],
  );
  const terminalSetupAction = useMemo(
    () => setupActions.find(isTerminalSetupAction) ?? null,
    [setupActions],
  );
  const consoleSetupActions = useMemo(
    () => setupActions.filter((action) => !isTerminalSetupAction(action)),
    [setupActions],
  );
  const sharedServices = useMemo(
    () => terminalSetupAction
      ? []
      : (schema?.shared_services?.length ? schema.shared_services : []),
    [schema, terminalSetupAction],
  );
  const stepTitles = useMemo(
    () => terminalSetupAction
      ? ["填写配置", "在终端完成部署"]
      : [
          "填写配置",
          ...consoleSetupActions.map((action) => action.label),
          ...sharedServices.map((service) => service.label),
          "同步代码",
          "重建环境",
          "注册服务",
          "启动",
        ],
    [consoleSetupActions, sharedServices, terminalSetupAction],
  );
  const sharedStartIdx = 1 + consoleSetupActions.length;
  const syncIdx = sharedStartIdx + sharedServices.length;
  const rebuildIdx = syncIdx + 1;
  const registerIdx = rebuildIdx + 1;
  const startIdx = registerIdx + 1;

  const [current, setCurrent] = useState(0);
  const [states, setStates] = useState<StepState[]>(
    stepTitles.map((_, i) => (i === 0 ? "process" : "wait")),
  );
  const [lines, setLines] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [xhsQrOpen, setXhsQrOpen] = useState(false);
  const [xhsQrLoading, setXhsQrLoading] = useState(false);
  const [xhsQrDataUrl, setXhsQrDataUrl] = useState("");
  const [xhsLoginMessage, setXhsLoginMessage] = useState("");
  const closer = useRef<(() => void) | null>(null);

  useEffect(() => {
    let cancelled = false;
    setSchema(null);
    setSchemaError("");
    api
      .provisionSchema(bot.instance_id)
      .then((next) => {
        if (!cancelled) setSchema(next);
      })
      .catch((e) => {
        if (!cancelled) setSchemaError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [bot.instance_id]);

  useEffect(() => {
    setCurrent(0);
    setStates(stepTitles.map((_, i) => (i === 0 ? "process" : "wait")));
    setXhsQrOpen(false);
    setXhsQrDataUrl("");
    setXhsLoginMessage("");
  }, [bot.instance_id, stepTitles]);

  const mark = (idx: number, s: StepState) =>
    setStates((prev) => prev.map((v, i) => (i === idx ? s : v)));

  const advance = (idx: number) => {
    mark(idx, "finish");
    const next = idx + 1;
    if (next < stepTitles.length) {
      setCurrent(next);
      mark(next, "process");
    }
  };

  const runStreamStep = async (idx: number, start: () => Promise<Task>) => {
    setBusy(true);
    mark(idx, "process");
    setLines([]);
    try {
      const task = await start();
      await new Promise<void>((resolve) => {
        closer.current = streamTask(
          task.id,
          (line) => setLines((prev) => [...prev, line]),
          () => resolve(),
        );
      });
      const finished = await api.task(task.id);
      if (finished.status === "done") {
        advance(idx);
      } else {
        mark(idx, "error");
        Message.error(`${stepTitles[idx]} 失败，exit ${finished.exit_code ?? "?"}`);
      }
    } catch (e) {
      mark(idx, "error");
      Message.error(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const submitEnv = async (values: ProvisionEnvPayload) => {
    setBusy(true);
    mark(0, "process");
    try {
      const res = await api.provisionEnv(bot.instance_id, values);
      setLines([
        `[OK] 本地配置已写入 ${res.local_env_file ?? "bots/<id>/local.env"}`,
        `[OK] 运行时 env 已生成 ${res.env_file}`,
        res.written_keys.length
          ? `更新键：${res.written_keys.join(", ")}`
          : "配置内容未变化；已保留现有值。",
      ]);
      advance(0);
    } catch (e) {
      mark(0, "error");
      Message.error(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const runSetupAction = (idx: number, action: SetupAction) => {
    void runStreamStep(
      idx,
      () => api.setupAction(bot.instance_id, action.id, setupActionVerb(action)),
    );
  };

  const fetchXhsQrcode = async (): Promise<boolean> => {
    setXhsQrLoading(true);
    try {
      const qr = await api.xhsLoginQrcode();
      if (qr.already_logged_in) {
        setXhsLoginMessage(qr.message || "已处于登录状态，无需再次登录。");
        return true;
      }
      setXhsQrDataUrl(qr.image_data_url ?? "");
      setXhsLoginMessage("请用小红书 App 扫码登录。");
      return true;
    } catch (e) {
      const message = e instanceof Error ? e.message : String(e);
      setXhsLoginMessage(message);
      Message.error(message);
      return false;
    } finally {
      setXhsQrLoading(false);
    }
  };

  const runSharedService = async (idx: number, service: SharedServiceStep) => {
    if (service.service !== "xhs") {
      mark(idx, "error");
      Message.error(`不支持的共享服务：${service.service}`);
      return;
    }
    setBusy(true);
    mark(idx, "process");
    setLines([]);
    try {
      const started = await api.sharedServiceXhsStart();
      setLines([
        "[console] 启动小红书 MCP 服务",
        started.stdout || "[OK] xiaohongshu-mcp started",
        started.stderr || "",
      ].filter(Boolean));
      setXhsQrOpen(true);
      const ok = await fetchXhsQrcode();
      if (!ok) {
        mark(idx, "error");
      }
    } catch (e) {
      mark(idx, "error");
      Message.error(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const checkXhsLogin = async () => {
    setXhsQrLoading(true);
    try {
      const status = await api.xhsCheckLogin();
      setXhsLoginMessage(status.message || (status.logged_in ? "已登录。" : "尚未登录，请扫码后重试。"));
      if (status.logged_in) {
        setXhsQrOpen(false);
        advance(current);
        Message.success("小红书已登录");
      } else {
        mark(current, "error");
        Message.warning("尚未检测到小红书登录");
      }
    } catch (e) {
      const message = e instanceof Error ? e.message : String(e);
      setXhsLoginMessage(message);
      mark(current, "error");
      Message.error(message);
    } finally {
      setXhsQrLoading(false);
    }
  };

  const doStart = async () => {
    setBusy(true);
    mark(startIdx, "process");
    try {
      await api.control(bot.instance_id, "start");
      mark(startIdx, "finish");
      setLines([`[OK] ${bot.instance_id} 已启动`]);
      Message.success("已启动");
      onChanged();
    } catch (e) {
      mark(startIdx, "error");
      Message.error(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const close = () => {
    closer.current?.();
    onClose();
  };

  const fields = schema
    ? editableProvisionFields([...schema.fields, ...schema.common_fields])
    : [];
  const currentSetupAction = current > 0 && current <= consoleSetupActions.length
    ? consoleSetupActions[current - 1]
    : null;
  const currentSharedService =
    current >= sharedStartIdx && current < syncIdx
      ? sharedServices[current - sharedStartIdx]
      : null;

  return (
    <div>
      <Alert
        type="info"
        closable={false}
        content={`首次部署 ${bot.display_name}：${stepTitles.join(" -> ")}`}
        className="block-gap-bottom"
        showIcon
      />
      <Steps current={current} size="small" className="provision-steps">
        {stepTitles.map((title, i) => (
          <Steps.Step key={`${i}-${title}`} title={title} status={states[i]} />
        ))}
      </Steps>

      {current === 0 ? (
        <Form onSubmit={(v) => submitEnv(v as ProvisionEnvPayload)} layout="vertical">
          {schemaError ? (
            <Alert type="warning" closable={false} content={schemaError} showIcon />
          ) : null}
          {fields.length ? (
            fields.map(renderField)
          ) : (
            <Text type="secondary">正在读取平台配置...</Text>
          )}
          <Space className="panel-action-row">
            <Button
              type="primary"
              htmlType="submit"
              loading={busy}
              disabled={!fields.length}
            >
              保存本机配置并继续
            </Button>
            <Button onClick={close}>关闭</Button>
          </Space>
          <Text type="secondary" className="cc-text-small form-help-text">
            本机私有配置会保存到该 bot 目录的 local.env，并生成 {bot.env_file}。
          </Text>
        </Form>
      ) : (
        <div>
          <LogConsole lines={lines} />
          {terminalSetupAction ? (
            <Alert
              type="warning"
              closable={false}
              showIcon
              className="block-gap-bottom"
              content={(
                <div>
                  <div>QQ 登录、Docker 与 systemd 部署需要在 WSL/Linux 终端继续完成：</div>
                  <Input
                    readOnly
                    value={terminalQuickstartCommand(bot.instance_id)}
                    className="block-gap-top"
                  />
                  <Text type="secondary" className="cc-text-small form-help-text">
                    终端向导会从现有 local.env 恢复；Console 不会直接启动 QQ gateway。
                  </Text>
                </div>
              )}
            />
          ) : null}
          <Space className="panel-action-row-large">
            {!terminalSetupAction && currentSetupAction && (
              <Button
                type="primary"
                loading={busy}
                onClick={() => runSetupAction(current, currentSetupAction)}
              >
                {states[current] === "error" ? `重试 ${currentSetupAction.label}` : currentSetupAction.label}
              </Button>
            )}
            {!terminalSetupAction && currentSharedService && (
              <Button
                type="primary"
                loading={busy}
                onClick={() => runSharedService(current, currentSharedService)}
              >
                {states[current] === "error" ? `重试 ${currentSharedService.label}` : currentSharedService.label}
              </Button>
            )}
            {!terminalSetupAction && current === syncIdx && (
              <Button
                type="primary"
                loading={busy}
                onClick={() => runStreamStep(syncIdx, () => api.sync(bot.instance_id, false))}
              >
                {states[syncIdx] === "error" ? "重试同步代码" : "同步代码"}
              </Button>
            )}
            {!terminalSetupAction && current === rebuildIdx && (
              <Button
                type="primary"
                loading={busy}
                onClick={() => runStreamStep(rebuildIdx, () => api.rebuild(bot.instance_id, false))}
              >
                {states[rebuildIdx] === "error" ? "重试重建环境" : "重建环境"}
              </Button>
            )}
            {!terminalSetupAction && current === registerIdx && (
              <Button
                type="primary"
                loading={busy}
                onClick={() => runStreamStep(registerIdx, () => api.register(bot.instance_id))}
              >
                {states[registerIdx] === "error" ? "重试注册服务" : "注册服务"}
              </Button>
            )}
            {!terminalSetupAction && current === startIdx && (
              <Button type="primary" loading={busy} onClick={doStart}>
                {states[startIdx] === "finish" ? "已启动" : "启动"}
              </Button>
            )}
            <Button onClick={close}>
              {!terminalSetupAction && states[startIdx] === "finish" ? "完成" : "关闭"}
            </Button>
          </Space>
        </div>
      )}
      <Modal
        title="小红书扫码登录"
        visible={xhsQrOpen}
        style={{ width: 420 }}
        okText="检查登录"
        cancelText="关闭"
        confirmLoading={xhsQrLoading}
        onOk={() => void checkXhsLogin()}
        onCancel={() => setXhsQrOpen(false)}
      >
        <div className="provision-qrcode-body">
          {xhsQrLoading && !xhsQrDataUrl ? (
            <Spin size={28} className="provision-qrcode-spinner" />
          ) : xhsQrDataUrl ? (
            <img
              src={xhsQrDataUrl}
              alt="小红书登录二维码"
              className="provision-qrcode-image"
            />
          ) : (
            <Text type="secondary">暂无二维码</Text>
          )}
          <Text type="secondary" className="provision-qrcode-message">
            {xhsLoginMessage || "请用小红书 App 扫码登录。"}
          </Text>
          <Space>
            <Button loading={xhsQrLoading} onClick={() => void fetchXhsQrcode()}>
              刷新二维码
            </Button>
            <Button type="primary" loading={xhsQrLoading} onClick={() => void checkXhsLogin()}>
              检查登录
            </Button>
          </Space>
        </div>
      </Modal>
    </div>
  );
}
