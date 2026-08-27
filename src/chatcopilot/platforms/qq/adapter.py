"""QQ 平台适配器（cc-connect@beta, OneBot v11 / NapCat）。

链路：``QQ Client <-> NapCat -> QQ @ Relay -> cc-connect -> ACP server``。
平台层只承载 QQ 会话身份解析、文件回传与部署渲染；具体机器人实例是否启用
per-user workspace 附件流水线，由 ``bots/<bot-id>/bot.yaml`` 的 ``tools.features``
（如 ``chat.file_uploads`` / ``chat.private_workspace``）决定。

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
    """基于 cc-connect OneBot v11（NapCat）通道的 QQ 适配器。"""

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
                "QQ_ACCOUNT",
                required=True,
                label="机器人 QQ 号",
                description="用于 @ 识别与 NapCat 登录的稳定数字 ID",
            ),
            SecretSpec(
                "QQ_WS_URL",
                required=False,
                default="ws://127.0.0.1:3001",
                label="OneBot WebSocket 地址",
                description="NapCat 正向 WebSocket 地址（OneBot v11）",
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
                description="ACP QQ 用户准入名单（空值不授予权限）",
            ),
            SecretSpec(
                "QQ_ALLOW_GROUPS",
                required=False,
                default="",
                label="允许接入的 QQ 群",
                description="ACP QQ 群准入名单（空值不授予权限）",
            ),
            SecretSpec(
                "QQ_AT_PROXY_URL",
                required=False,
                default="ws://127.0.0.1:3002",
                label="QQ @ Relay 地址",
                description="QQ @ Relay 监听地址；cc-connect 固定连接此回环地址",
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
        if not is_numeric_platform_id(env.get("QQ_ACCOUNT")):
            errors.append(
                "qq_account_invalid: QQ_ACCOUNT must be a numeric QQ account for the mention relay"
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
        try:
            require_loopback_websocket_url(
                env.get("QQ_WS_URL") or "ws://127.0.0.1:3001",
                env_key="QQ_WS_URL",
            )
        except QQBoundaryError as exc:
            errors.append(f"{exc.error_code}: {exc}")
        try:
            require_loopback_websocket_url(
                env.get("QQ_AT_PROXY_URL") or "ws://127.0.0.1:3002",
                env_key="QQ_AT_PROXY_URL",
            )
        except QQBoundaryError as exc:
            errors.append(f"{exc.error_code}: {exc}")
        return tuple(errors)

    def materialize_host_generated_secret(
        self,
        env_key: str,
        current_value: str,
    ) -> str:
        if env_key != "QQ_ACCESS_TOKEN":
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
