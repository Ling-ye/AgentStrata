"""QQ deployment adapter for the Gateway-owned OneBot v11 Channel.

The production path is ``QQ Client <-> external OneBot provider <-> Gateway``.
The Gateway owns transport and Channel lifecycle; authorization remains in the
host policy layer. Legacy runtime helpers remain isolated during the cutover and
are not selected by a Gateway BotSpec.

后台任务通知通过 NapCat OneBot v11 ``send_msg`` 主动发送到 workspace 对应的原私聊或群聊。

在模块级暴露 ``ADAPTER`` 供 ``platforms.registry`` 自动发现。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import secrets
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from chatcopilot.core.allowlists import (
    AllowlistConfigError,
    is_numeric_platform_id,
    parse_numeric_allowlist,
)
from chatcopilot.platforms.base import (
    ExternalCheckReport,
    PlatformAdapter,
    SecretSpec,
    SessionIdentity,
    SetupActionSpec,
)
from chatcopilot.platforms.qq import notifier as _notifier
from chatcopilot.platforms.qq import sender as _sender
from chatcopilot.platforms.qq.boundary import (
    QQBoundaryError,
    require_access_token,
    require_loopback_websocket_url,
)
from chatcopilot.platforms.qq.gateway_health import run_qq_external_checks

if TYPE_CHECKING:
    from chatcopilot.contracts.workspace import WorkspaceView as Workspace


class QQAdapter(PlatformAdapter):
    """Expose QQ provisioning and external checks for the Gateway Channel."""

    name = "qq"
    adapter_id = "qq_acp"

    supports_role_matrix = False
    supports_user_files_pipeline = False
    supports_background_jobs = True
    allow_role_name_match = False
    group_conversation_scope = "chat"
    requires_sender_envelope = True

    # -- runtime: identity --------------------------------------------------
    def parse_session_identity(
        self,
        *,
        session_key: str,
        hook_user_id: str | None = None,
        hook_chat_id: str | None = None,
        hook_chat_kind: str | None = None,
        hook_user_name: str | None = None,
    ) -> SessionIdentity:
        user_id = (hook_user_id or "").strip()
        chat_id = (hook_chat_id or "").strip()
        chat_kind = (hook_chat_kind or "").strip()
        user_name = (hook_user_name or "").strip()

        parts = [part.strip() for part in (session_key or "").split(":") if part.strip()]
        if parts and parts[0] == self.name:
            parts = parts[1:]

        if len(parts) >= 2 and parts[0] == "g":
            # The shared session key is the authoritative conversation scope.
            # Hook fields describe an event and may be missing or stale.
            chat_id = parts[1]
            chat_kind = "group"
            # Shared session identity is the conversation, never the hook actor.
            # Per-turn actor identity comes from the sender envelope bound to the prompt.
            user_id = ""
            user_name = ""
        elif len(parts) >= 2:
            if not chat_id:
                chat_id = parts[0]
            if not user_id:
                user_id = parts[1]
            if not chat_kind:
                chat_kind = "group"
        elif len(parts) == 1:
            if not user_id:
                user_id = parts[0]
            if not chat_kind:
                chat_kind = "p2p"

        if not chat_kind:
            chat_kind = "group" if chat_id else "p2p"

        return SessionIdentity(
            user_id=user_id or None,
            chat_id=chat_id or None,
            chat_kind=chat_kind or None,
            user_name=user_name or None,
        )

    # -- runtime: file delivery --------------------------------------------
    def resolve_sendable_paths(self, workspace: "Workspace", files: Sequence[str]) -> list[Path]:
        return _sender.resolve_sendable_paths(workspace, files)

    def send_files(
        self,
        files: Sequence[Path],
        *,
        message: str = "",
        workspace: "Workspace | None" = None,
    ) -> str:
        return _sender.send_via_cc_connect(
            files,
            message=message,
            workspace=workspace,
        )

    def send_workspace_files(
        self,
        workspace: "Workspace",
        files: Sequence[Path],
        *,
        message: str = "",
    ) -> str:
        return self.send_files(files, message=message, workspace=workspace)

    # -- runtime: background notification ----------------------------------
    def resolve_delivery_target(self, workspace: "Workspace") -> Any:
        return _notifier.resolve_delivery_target(workspace)

    def send_text(self, workspace: "Workspace", text: str, *, timeout: int = 10) -> Any:
        return _notifier.send_text_to_workspace(workspace, text, timeout=timeout)

    # -- deploy -------------------------------------------------------------
    def required_secrets(self) -> tuple[SecretSpec, ...]:
        return (
            SecretSpec(
                "CHATCOPILOT_GATEWAY_PORT",
                required=False,
                default="18789",
                label="Gateway 端口",
                description="每个 Bot 的回环 Gateway 监听端口",
            ),
            SecretSpec(
                "CHATCOPILOT_GATEWAY_TOKEN",
                required=True,
                label="Gateway Access Token",
                description="Gateway 客户端凭据（32–128 位 URL-safe 字符）",
                host_generated=True,
            ),
            SecretSpec(
                "QQ_ACCOUNT",
                required=True,
                label="机器人 QQ 号",
                description="用于账号校验与结构化 @ 识别的稳定数字 ID",
            ),
            SecretSpec(
                "CHATCOPILOT_QQ_ONEBOT_WS_URL",
                required=False,
                default="ws://127.0.0.1:3001",
                label="OneBot WebSocket 地址",
                description="外部 OneBot v11 provider 的回环 WebSocket 地址",
            ),
            SecretSpec(
                "QQ_ACCESS_TOKEN",
                required=True,
                label="OneBot Access Token",
                description="OneBot access token（32–128 位 URL-safe 字符）",
                host_generated=True,
            ),
            SecretSpec(
                "QQ_ALLOW_FROM",
                required=False,
                default="",
                label="允许接入的 QQ 用户",
                description="Gateway QQ 用户准入名单（空值不授予权限）",
            ),
            SecretSpec(
                "QQ_ALLOW_GROUPS",
                required=False,
                default="",
                label="允许接入的 QQ 群",
                description="Gateway QQ 群准入名单（空值不授予权限）",
            ),
            SecretSpec(
                "QQ_WEBUI_PORT",
                required=False,
                default="6099",
                label="NapCat WebUI 端口",
                description="仅绑定本机回环地址的 WebUI 端口",
            ),
            SecretSpec(
                "QQ_IMAGE_MAX_BYTES",
                required=False,
                default="5242880",
                label="QQ 图片大小上限",
                description="QQ image max bytes",
            ),
            SecretSpec(
                "QQ_IMAGE_SEND_TIMEOUT_SECONDS",
                required=False,
                default="15",
                label="QQ 图片发送超时",
                description="QQ image send timeout seconds",
            ),
        )

    def validate_runtime_env(self, env: Mapping[str, str]) -> tuple[str, ...]:
        errors: list[str] = []
        for legacy_key in (
            "QQ_WS_URL",
            "QQ_AT_PROXY_URL",
            "CHATCOPILOT_CC_CONNECT_BIN",
            "CHATCOPILOT_CC_HOME",
            "CHATCOPILOT_CC_CONNECT_CONFIG_DIR",
            "CHATCOPILOT_SESSION_ENV_DIR",
        ):
            if legacy_key in env:
                errors.append(
                    "qq_legacy_gateway_env_removed: "
                    f"{legacy_key} is not accepted by the Gateway QQ runtime"
                )
        if not is_numeric_platform_id(env.get("QQ_ACCOUNT")):
            errors.append(
                "qq_account_invalid: QQ_ACCOUNT must be a numeric QQ account"
            )
        for legacy_key in ("QQ_REQUIRE_AT_IN_GROUP", "QQ_AT_ALL_COUNTS"):
            if legacy_key in env:
                errors.append(
                    f"qq_legacy_ingress_env_removed: {legacy_key} is no longer supported"
                )
        for allowlist_key in ("QQ_ALLOW_FROM", "QQ_ALLOW_GROUPS"):
            try:
                parse_numeric_allowlist(env.get(allowlist_key), field=allowlist_key)
            except AllowlistConfigError as exc:
                errors.append(f"qq_allowlist_invalid: {exc}")
        try:
            require_access_token(env.get("QQ_ACCESS_TOKEN"))
        except QQBoundaryError as exc:
            errors.append(f"{exc.error_code}: {exc}")
        gateway_token = str(env.get("CHATCOPILOT_GATEWAY_TOKEN", "") or "").strip()
        try:
            require_access_token(gateway_token)
        except QQBoundaryError:
            errors.append(
                "gateway_access_token_invalid: CHATCOPILOT_GATEWAY_TOKEN must be "
                "32-128 URL-safe characters"
            )
        if gateway_token and gateway_token == str(env.get("QQ_ACCESS_TOKEN", "") or "").strip():
            errors.append(
                "gateway_token_reused: CHATCOPILOT_GATEWAY_TOKEN must differ from QQ_ACCESS_TOKEN"
            )
        try:
            require_loopback_websocket_url(
                env.get("CHATCOPILOT_QQ_ONEBOT_WS_URL") or "ws://127.0.0.1:3001",
                env_key="CHATCOPILOT_QQ_ONEBOT_WS_URL",
            )
        except QQBoundaryError as exc:
            errors.append(f"{exc.error_code}: {exc}")
        port_text = str(env.get("CHATCOPILOT_GATEWAY_PORT", "18789") or "").strip()
        try:
            port = int(port_text)
        except ValueError:
            port = 0
        if not 1 <= port <= 65535:
            errors.append(
                "gateway_port_invalid: CHATCOPILOT_GATEWAY_PORT must be an integer from 1 to 65535"
            )
        return tuple(errors)

    def materialize_host_generated_secret(
        self,
        env_key: str,
        current_value: str,
    ) -> str:
        if env_key not in {"QQ_ACCESS_TOKEN", "CHATCOPILOT_GATEWAY_TOKEN"}:
            return super().materialize_host_generated_secret(env_key, current_value)
        try:
            require_access_token(current_value)
        except QQBoundaryError:
            return secrets.token_urlsafe(32)
        return current_value

    def setup_actions(self) -> tuple[SetupActionSpec, ...]:
        return (
            SetupActionSpec(
                id="qq-gateway",
                label="QQ gateway",
                description="Start/check NapCat and the OneBot @ Relay for QQ deployments.",
                command=(
                    "bash",
                    "{repo_root}/deploy/wsl/qq_gateway.sh",
                    "{verb}",
                    "--instance",
                    "{instance_id}",
                ),
                allowed_verbs=(
                    "bootstrap",
                    "sync-token",
                    "start",
                    "restart",
                    "status",
                    "logs",
                ),
                guided_surface="terminal",
                default_verb="bootstrap",
            ),
        )

    def run_external_checks(
        self,
        env: Mapping[str, str],
        *,
        bot_id: str,
        send_message: bool = False,
        confirm_external_write: bool = False,
    ) -> ExternalCheckReport:
        return asyncio.run(
            run_qq_external_checks(
                env,
                bot_id=bot_id,
                send_message=send_message,
                confirm_external_write=confirm_external_write,
            )
        )

    def render_cc_connect_section(self, env: Mapping[str, str]) -> str:
        errors = self.validate_runtime_env(env)
        if errors:
            raise ValueError("; ".join(errors))
        token = require_access_token(env.get("QQ_ACCESS_TOKEN"))
        proxy_url = env.get("QQ_AT_PROXY_URL") or "ws://127.0.0.1:3002"
        return (
            "[[projects.platforms]]\n"
            "# QQ (cc-connect@beta, OneBot v11 / NapCat)。cc-connect 经正向 WebSocket 连本机\n"
            "# QQ 登录态由 NapCat 持有（首次需手机扫码）。ws_url 指向 Relay，\n"
            "# token 由 env file 注入，allow_from 固定放行到 ACP。\n"
            'type = "qq"\n'
            "\n"
            "[projects.platforms.options]\n"
            "# ws_url 固定指向本机 QQ @ Relay；Relay 只保留群聊明确 @ 的传输触发。\n"
            f"ws_url = {json.dumps(proxy_url, ensure_ascii=False)}\n"
            f"token = {json.dumps(token, ensure_ascii=False)}\n"
            'allow_from = "*"\n'
            "share_session_in_channel = true\n"
            "\n"
        )


ADAPTER = QQAdapter()


__all__ = ["ADAPTER", "QQAdapter"]
