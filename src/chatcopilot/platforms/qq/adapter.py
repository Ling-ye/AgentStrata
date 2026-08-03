"""QQ 平台适配器（cc-connect@beta, OneBot v11 / NapCat）。

链路：``QQ Client <-> NapCat (OneBot v11) <-WebSocket-> cc-connect <-> ACP server``。
平台层只承载 QQ 会话身份解析、文件回传与部署渲染；具体机器人实例是否启用
per-user workspace 附件流水线，由 ``bots/<bot-id>/bot.yaml`` 的 ``tools.features``
（如 ``chat.file_uploads`` / ``chat.private_workspace``）决定。

后台任务通知通过 NapCat OneBot v11 ``send_msg`` 主动发送到 workspace 对应的原私聊或群聊。

在模块级暴露 ``ADAPTER`` 供 ``platforms.registry`` 自动发现。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from chatcopilot.platforms.base import (
    PlatformAdapter,
    SecretSpec,
    SessionIdentity,
    SetupActionSpec,
)
from chatcopilot.platforms.qq import notifier as _notifier
from chatcopilot.platforms.qq import sender as _sender
from chatcopilot.platforms.qq.gateway_health import (
    QQBoundaryError,
    require_access_token,
    require_loopback_websocket_url,
)

if TYPE_CHECKING:
    from chatcopilot.contracts.workspace import WorkspaceView as Workspace


def _as_bool(value: str | None) -> bool:
    """把 env 字符串归一化成 bool；缺省 / 无法识别一律 False。"""
    if not value:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


class QQAdapter(PlatformAdapter):
    """基于 cc-connect OneBot v11（NapCat）通道的 QQ 适配器。"""

    name = "qq"
    adapter_id = "qq_acp"

    supports_role_matrix = False
    supports_user_files_pipeline = False
    supports_background_jobs = True
    allow_role_name_match = False

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

        if len(parts) >= 2:
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

    def send_files(self, files: Sequence[Path], *, message: str = "") -> str:
        return _sender.send_via_cc_connect(files, message=message)

    # -- runtime: background notification ----------------------------------
    def resolve_delivery_target(self, workspace: "Workspace") -> Any:
        return _notifier.resolve_delivery_target(workspace)

    def send_text(self, workspace: "Workspace", text: str, *, timeout: int = 10) -> Any:
        return _notifier.send_text_to_workspace(workspace, text, timeout=timeout)

    # -- deploy -------------------------------------------------------------
    def required_secrets(self) -> tuple[SecretSpec, ...]:
        return (
            SecretSpec("QQ_ACCOUNT", required=True, description="机器人 QQ 号，用于 @ 识别与 NapCat 登录"),
            SecretSpec(
                "QQ_WS_URL",
                required=False,
                default="ws://127.0.0.1:3001",
                description="NapCat 正向 WebSocket 地址（OneBot v11）",
            ),
            SecretSpec(
                "QQ_ACCESS_TOKEN",
                required=True,
                description="OneBot access token（32–128 位 URL-safe 字符）",
            ),
            SecretSpec("QQ_ALLOW_FROM", required=False, default="*", description="允许的来源白名单（默认全部）"),
            SecretSpec(
                "QQ_REQUIRE_AT_IN_GROUP",
                required=False,
                default="true",
                description="群聊是否必须 @机器人 才回（默认 true，经 OneBot @ 过滤代理实现）",
            ),
            SecretSpec(
                "QQ_AT_PROXY_URL",
                required=False,
                default="ws://127.0.0.1:3002",
                description="@ 过滤代理监听地址；启用群聊 @门禁时 cc-connect 连这里而非直连 NapCat",
            ),
            SecretSpec("QQ_WEBUI_PORT", required=False, default="6099", description="NapCat WebUI 端口"),
            SecretSpec("QQ_IMAGE_MAX_BYTES", required=False, default="5242880", description="QQ image max bytes"),
            SecretSpec("QQ_IMAGE_SEND_TIMEOUT_SECONDS", required=False, default="15", description="QQ image send timeout seconds"),
        )

    def validate_runtime_env(self, env: Mapping[str, str]) -> tuple[str, ...]:
        errors: list[str] = []
        require_at = str(env.get("QQ_REQUIRE_AT_IN_GROUP") or "true").strip().lower()
        if require_at not in {"1", "true", "yes", "on", "0", "false", "no", "off"}:
            errors.append(
                "qq_require_at_invalid: QQ_REQUIRE_AT_IN_GROUP must be a boolean"
            )
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

    def setup_actions(self) -> tuple[SetupActionSpec, ...]:
        return (
            SetupActionSpec(
                id="qq-gateway",
                label="QQ gateway",
                description="Start/check NapCat and the OneBot @ proxy for QQ deployments.",
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
            ),
        )

    def render_cc_connect_section(self, env: Mapping[str, str]) -> str:
        errors = self.validate_runtime_env(env)
        if errors:
            raise ValueError("; ".join(errors))
        napcat_url = env.get("QQ_WS_URL") or "ws://127.0.0.1:3001"
        token = require_access_token(env.get("QQ_ACCESS_TOKEN"))
        allow_from = env.get("QQ_ALLOW_FROM") or "*"
        # 群聊 @门禁：cc-connect 的 NapCat-QQ 适配器会丢弃 @ 段、且不判 @（源码 platform/qq/qq.go），
        # 无配置可改。故启用时让 cc-connect 连我们的 OneBot @ 过滤代理（QQ_AT_PROXY_URL），由代理
        # 在转发前丢弃"群聊未 @机器人"的事件；关闭时直连 NapCat（QQ_WS_URL）。
        require_at = _as_bool(env.get("QQ_REQUIRE_AT_IN_GROUP", "true"))
        proxy_url = env.get("QQ_AT_PROXY_URL") or "ws://127.0.0.1:3002"
        ws_url = proxy_url if require_at else napcat_url
        ws_comment = (
            "# ws_url 指向本仓库的 OneBot @ 过滤代理（群聊必须 @机器人 才回）；代理上游连 NapCat。\n"
            if require_at
            else "# ws_url 直连 NapCat（未启用群聊 @门禁）。\n"
        )
        return (
            "[[projects.platforms]]\n"
            "# QQ (cc-connect@beta, OneBot v11 / NapCat)。cc-connect 经正向 WebSocket 连本机\n"
            "# NapCat；QQ 登录态由 NapCat 持有（首次需手机扫码）。ws_url / token / allow_from\n"
            "# 由 env file 注入。\n"
            'type = "qq"\n'
            "\n"
            "[projects.platforms.options]\n"
            + ws_comment
            + f"ws_url = {json.dumps(ws_url, ensure_ascii=False)}\n"
            f"token = {json.dumps(token, ensure_ascii=False)}\n"
            f"allow_from = {json.dumps(allow_from, ensure_ascii=False)}\n"
            "\n"
        )


ADAPTER = QQAdapter()


__all__ = ["ADAPTER", "QQAdapter"]
