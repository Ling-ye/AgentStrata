import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Input, Modal, Space, Spin, Tag } from "@arco-design/web-react";
import { api } from "../../api";
import { weixinErrorLabel, weixinLoginActive, weixinLoginLabels } from "./weixinModel";

export default function WeixinBinding({ instanceId, running }: { instanceId: string; running?: boolean }) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [loginId, setLoginId] = useState<string | null>(null);
  const [code, setCode] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const generation = useRef(0);
  const active = useRef<string | null>(null);
  const binding = useQuery({ queryKey: ["weixin-status", instanceId], queryFn: ({ signal }) => api.weixinStatus(instanceId, signal),
    refetchInterval: 5000 });
  const login = useQuery({ queryKey: ["weixin-login", instanceId, loginId], enabled: open && !!loginId,
    queryFn: ({ signal }) => api.weixinLoginStatus(instanceId, loginId!, signal),
    refetchInterval: (query) => query.state.data && !weixinLoginActive(query.state.data.status) ? false : 1000 });

  useEffect(() => {
    if (login.data && !weixinLoginActive(login.data.status)) {
      active.current = null;
      void queryClient.invalidateQueries({ queryKey: ["weixin-status", instanceId] });
    }
  }, [login.data?.status, instanceId, queryClient]);
  useEffect(() => () => {
    generation.current++;
    if (active.current) void api.weixinCancel(instanceId, active.current).catch(() => undefined);
  }, [instanceId]);

  const close = async () => {
    generation.current++;
    if (active.current) {
      setBusy(true);
      try { await api.weixinCancel(instanceId, active.current); }
      catch (e) { setError(weixinErrorLabel(e)); setBusy(false); return; }
    }
    active.current = null;
    setOpen(false);
    setLoginId(null);
    setCode("");
    setBusy(false);
  };
  const start = async () => {
    const revision = ++generation.current;
    setError(null);
    setBusy(true);
    setOpen(true);
    try {
      const result = await api.weixinLogin(instanceId);
      if (revision !== generation.current) {
        void api.weixinCancel(instanceId, result.login_id).catch(() => undefined);
        return;
      }
      active.current = result.login_id;
      setLoginId(result.login_id);
      queryClient.setQueryData(["weixin-login", instanceId, result.login_id], result);
    } catch (e) {
      if (revision === generation.current) setError(weixinErrorLabel(e));
    } finally {
      if (revision === generation.current) setBusy(false);
    }
  };
  const verify = async () => {
    if (!loginId) return;
    const revision = generation.current;
    setBusy(true);
    setError(null);
    try {
      await api.weixinVerify(instanceId, loginId, code);
      if (revision === generation.current) {
        setCode("");
        void login.refetch();
      }
    } catch (e) {
      if (revision === generation.current) setError(weixinErrorLabel(e));
    } finally {
      if (revision === generation.current) setBusy(false);
    }
  };
  return <>
    <Tag color={binding.data?.connected ? "green" : "gray"}>
      {binding.data?.connected ? "微信已连接" : binding.data?.connection_state === "error" ? "微信连接异常" : binding.data?.connection_state === "unknown" ? "微信连接状态未知" : binding.data?.bound ? "微信已绑定" : "微信未绑定"}
    </Tag>
    <Button size="small" disabled={running || busy || binding.data?.login_active} onClick={() => void start()}>
      {binding.data?.bound ? "重新绑定微信" : "扫码绑定微信"}
    </Button>
    {running && <span>重新绑定前请先停止实例</span>}
    {binding.error && <span>微信状态读取失败</span>}
    <Modal title="绑定个人微信 ClawBot" style={{ width: "min(520px, calc(100vw - 32px))" }} visible={open} onCancel={() => void close()} footer={<Button loading={busy} onClick={() => void close()}>关闭</Button>}>
      <Alert type="info" content="使用本人微信扫码并确认。绑定成功后，本人将成为该微信实例的 Owner。" />
      {busy && <Spin />}
      {login.data?.qr_image && weixinLoginActive(login.data.status) &&
        <img src={login.data.qr_image} alt="微信绑定二维码" style={{ display: "block", width: "min(100%, 280px)", margin: "16px auto" }} />}
      <p>{weixinLoginLabels[login.data?.status ?? "creating"] ?? "未知绑定状态"}</p>
      {login.data?.status === "need_verifycode" && <Space wrap>
        <Input.Password placeholder="微信显示的验证码" value={code} onChange={setCode} autoComplete="one-time-code" />
        <Button loading={busy} disabled={!code} onClick={() => void verify()}>提交验证码</Button>
      </Space>}
      {(error || login.error || login.data?.error_code) && <Alert type="error" content={error || weixinErrorLabel(login.error || login.data?.error_code)} />}
      {login.data?.status === "confirmed" && <Alert type="success" content="凭据与 Owner 配置已保存。可通过实例启动入口启动微信机器人。" />}
      {login.data && ["expired", "failed", "save_failed", "cancelled"].includes(login.data.status) &&
        <Button onClick={() => { setLoginId(null); setCode(""); void start(); }}>重新生成二维码</Button>}
    </Modal>
  </>;
}
