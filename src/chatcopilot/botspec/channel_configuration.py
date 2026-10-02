"""Gateway-channel provisioning metadata; never a legacy runtime adapter."""

from __future__ import annotations

import re
import secrets

from chatcopilot.contracts.weixin import API_BASE, WeixinChannelConfig, WeixinError
from chatcopilot.platforms.base import SecretSpec


class WeixinConfiguration:
    name, adapter_id = "weixin", "gateway"

    def __init__(self, spec):
        self.spec = spec

    def required_secrets(self) -> tuple[SecretSpec, ...]:
        gateway, channel = self.spec.gateway, self.spec.channels.weixin
        return (
            SecretSpec(gateway.port_env, default="18790", label="Gateway 端口"),
            SecretSpec(gateway.token_env, host_generated=True, label="Gateway 凭据"),
            SecretSpec(gateway.state_root_env, required=False, label="实例私有状态目录"),
            SecretSpec(channel.access_token_env, required=False, label="微信 Bot 凭据（扫码保存）"),
            SecretSpec(channel.account_env, required=False, label="微信 Bot ID（扫码保存）"),
            SecretSpec(channel.user_env, required=False, label="微信用户 ID（扫码保存）"),
            SecretSpec(
                channel.endpoint_env, required=False, default=API_BASE, label="微信 API 地址"
            ),
        )

    def materialize_host_generated_secret(self, env_key, current_value):
        if env_key != self.spec.gateway.token_env:
            raise ValueError("host_generated_secret_unsupported")
        if current_value:
            if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", current_value):
                raise ValueError("gateway_token_invalid")
            return current_value
        return secrets.token_urlsafe(32)

    def validate_runtime_env(self, values) -> tuple[str, ...]:
        channel = self.spec.channels.weixin
        if not any(
            values.get(key)
            for key in (channel.access_token_env, channel.account_env, channel.user_env)
        ):
            return ()  # Provisioning a stopped, not-yet-bound instance is valid.
        try:
            WeixinChannelConfig(
                values.get(channel.account_env, ""),
                values.get(channel.user_env, ""),
                values.get(channel.access_token_env, ""),
                values.get(channel.endpoint_env, API_BASE),
            )
        except WeixinError as error:
            return (error.code,)
        return ()

    def setup_actions(self) -> tuple:
        return ()


def configuration_adapter(spec, *, resolver=None):
    if spec.platform.type == "weixin":
        return WeixinConfiguration(spec)
    from chatcopilot.platforms.registry import get_adapter

    return (resolver or get_adapter)(spec.platform.type)
