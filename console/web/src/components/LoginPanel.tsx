import { useCallback, useEffect, useState } from "react";
import { Button, Message, Space, Spin, Tag, Typography } from "@arco-design/web-react";
import { api } from "../api";
import { openNapcatLoginPage } from "../features/infra/napcatLogin";
import type { InfraService } from "../types";

const { Text } = Typography;

interface Props {
  service: InfraService;
}

export default function LoginPanel({ service }: Props) {
  const [open, setOpen] = useState(false);
  const [checking, setChecking] = useState(false);
  const [qrImage, setQrImage] = useState<string | null>(null);
  const [qrLoading, setQrLoading] = useState(false);
  const [loginState, setLoginState] = useState<"logged_in" | "logged_out" | null>(
    service.login_state,
  );
  useEffect(() => {
    setLoginState(service.login_state);
  }, [service.login_state]);

  const checkStatus = useCallback(async () => {
    setChecking(true);
    try {
      const res = await api.infraLoginCheck(service.id);
      if (res.logged_in) {
        setLoginState("logged_in");
        setQrImage(null);
        Message.success("已确认登录状态");
      } else {
        setLoginState("logged_out");
      }
    } catch (e) {
      Message.error(`检查失败：${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setChecking(false);
    }
  }, [service.id]);

  const fetchQrCode = async () => {
    setQrLoading(true);
    setQrImage(null);
    try {
      const res = await api.infraLoginQrcode(service.id);
      if (res.ok && res.already_logged_in) {
        setLoginState("logged_in");
        Message.success(res.message || "已处于登录状态");
      } else if (res.ok && res.image_data_url) {
        setQrImage(res.image_data_url);
        setLoginState("logged_out");
      } else {
        Message.error("获取二维码失败");
      }
    } catch (e) {
      Message.error(`获取二维码失败：${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setQrLoading(false);
    }
  };

  const handleLoginClick = async () => {
    if (service.login_type === "webui_link") {
      if (!openNapcatLoginPage(service.login_url)) {
        Message.error("NapCat 登录地址不可用，请确认对应实例已启动且 WebUI 端口配置有效");
      }
    } else if (service.login_type === "qrcode") {
      setOpen(true);
      if (loginState !== "logged_in") {
        await fetchQrCode();
      }
    }
  };

  const handleReLogin = () => {
    void fetchQrCode();
  };

  if (!service.has_login) return null;

  return (
    <div className="login-panel">
      <Space wrap>
        <Button
          size="small"
          onClick={handleLoginClick}
        >
          {service.login_type === "webui_link" ? "打开 NapCat 登录页" : "登录"}
        </Button>
        {service.login_type === "webui_link" && (
          <Button size="small" loading={checking} onClick={() => void checkStatus()}>
            检查登录状态
          </Button>
        )}
        {loginState === "logged_in" && (
          <Tag size="small" color="green">已登录</Tag>
        )}
        {loginState === "logged_out" && (
          <Tag size="small" color="red">未登录</Tag>
        )}
        {loginState === null && (
          <Tag size="small" color="orange">登录状态未知</Tag>
        )}
      </Space>

      {open && (
        <div className="login-panel-content">
          {service.login_type === "qrcode" && (
            <div className="login-panel-qrcode">
              {loginState === "logged_in" && !qrImage && (
                <div className="login-panel-logged-in">
                  <Tag color="green" size="large">已登录</Tag>
                  <Space className="panel-action-row">
                    <Button size="small" onClick={handleReLogin} loading={qrLoading}>
                      重新登录
                    </Button>
                    <Button size="small" type="text" onClick={() => setOpen(false)}>
                      收起
                    </Button>
                  </Space>
                </div>
              )}

              {(loginState !== "logged_in" || qrImage) && (
                <div className="login-panel-qr-area">
                  {qrLoading && !qrImage && (
                    <div className="login-panel-qrcode-loading">
                      <Spin tip="加载二维码..." />
                    </div>
                  )}
                  {qrImage && (
                    <>
                      <img
                        src={qrImage}
                        alt="登录二维码"
                        className="login-panel-qrcode-image"
                      />
                      <Text type="secondary" className="cc-text-small login-panel-qrcode-hint">
                        请使用对应 App 扫描二维码
                      </Text>
                    </>
                  )}
                  <Space className="panel-action-row">
                    <Button size="small" loading={checking} onClick={checkStatus}>
                      检查登录状态
                    </Button>
                    <Button size="small" loading={qrLoading} onClick={fetchQrCode}>
                      刷新二维码
                    </Button>
                    <Button size="small" type="text" onClick={() => setOpen(false)}>
                      收起
                    </Button>
                  </Space>
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
