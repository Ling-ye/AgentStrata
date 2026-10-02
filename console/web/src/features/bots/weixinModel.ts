export interface WeixinStatus {
  bound: boolean;
  account_id?: string;
  user_id?: string;
  connected: boolean;
  connection_state: string;
  login_active: boolean;
}

export interface WeixinLogin {
  login_id: string;
  status: string;
  qr_image?: string;
  error_code?: string;
  expires_at: number;
}

export const weixinLoginLabels: Record<string, string> = {
  creating: "正在生成二维码", wait: "等待扫码", scaned: "已扫码，请在微信确认",
  need_verifycode: "请输入微信显示的验证码", confirmed: "已绑定", expired: "二维码已过期",
  cancelled: "已取消", failed: "绑定失败", save_failed: "保存配置失败",
};

export function weixinLoginActive(status: string): boolean {
  return ["creating", "wait", "scaned", "need_verifycode"].includes(status);
}

const errors: Record<string, string> = {
  weixin_instance_running: "请先停止微信实例，再进行绑定。",
  weixin_login_active: "当前实例已有绑定或配置操作正在进行。",
  weixin_instance_busy: "实例仍在运行或由其他操作占用，请稍后重试。",
  weixin_account_replacement_requires_new_instance: "扫码账号与原绑定不同，请为新账号建立独立实例。",
  weixin_verification_invalid: "请重新输入微信显示的验证码。",
  weixin_credentials_save_failed: "配置保存失败，请检查实例私有配置后重试。",
  weixin_network_error: "微信服务连接失败，请检查网络后重试。",
  weixin_configuration_unavailable: "实例配置暂不可用，请检查配置后重试。",
};

export function weixinErrorLabel(error: unknown): string {
  const text = String(error);
  return Object.entries(errors).find(([code]) => text.includes(code))?.[1] ?? "微信绑定操作失败，请重试或检查实例配置。";
}
