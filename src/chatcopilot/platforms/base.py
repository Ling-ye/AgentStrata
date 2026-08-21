"""平台接入层的统一抽象契约。

一个渠道平台（飞书 / QQ / 微信 / ...）= 一个 :class:`PlatformAdapter` 子类。它把
原先散落在三处的平台知识聚合到一个对象上：

1. **运行时**：文件回传、后台任务通知、用户身份补全和平台技术能力声明。
2. **能力位**：是否启用角色矩阵 / 私聊文件流水线 / 后台任务通知，供 middleware 短路。
3. **部署**：声明该平台需要哪些 env 凭据，并渲染 cc-connect 的 ``[[projects.platforms]]``
   配置片段与额外配置文件（如飞书的 ``.lark-cli/config.json``）。

新增一个平台只需：

1. 在 ``platforms/<name>/`` 下写 ``adapter.py``，实现 ``PlatformAdapter`` 并在模块级
   暴露 ``ADAPTER = <YourAdapter>()``。
2. 不需要改任何注册表 / 白名单 / 部署脚本：``platforms.registry`` 目录扫描自动发现，
   ``botspec.loader`` 与 CLI / 部署脚本都从 registry 取支持列表与渲染逻辑。

middleware 与 agent 层仍不直接 import 具体平台模块；统一经 ``platforms.router``
（registry 的门面）按 ``BotSpec.platform.type`` 取到 adapter。
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Mapping, Sequence

from chatcopilot.contracts.identity import SessionIdentity

if TYPE_CHECKING:
    from chatcopilot.contracts.workspace import WorkspaceView as Workspace


# ---------------------------------------------------------------------------
# Platform-neutral message contracts（保留：供未来非 ACP 入站路径复用）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class InboundMessage:
    platform: str
    text: str
    user_id: str | None = None
    chat_id: str | None = None
    chat_kind: str | None = None
    resources: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OutboundMessage:
    text: str
    resources: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Deploy contract
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SecretSpec:
    """一个平台运行所需的 env 凭据声明。

    供 ``chatcopilot bot doctor`` 校验、CLI / 部署脚本渲染配置前的前置检查，
    以及运维控制台表单复用。``value`` 永远来自 env / 密钥管理器，不写进
    ``bot.yaml``、不进 git。
    """

    env_key: str
    required: bool = True
    default: str | None = None
    description: str = ""


@dataclass(frozen=True)
class SetupActionSpec:
    """Optional platform setup action exposed to deployment consoles."""

    id: str
    label: str
    description: str = ""
    command: tuple[str, ...] = ()
    allowed_verbs: tuple[str, ...] = ("start",)


ExternalCheckStatus = Literal[
    "passed",
    "failed",
    "error",
    "not_configured",
    "not_tested",
]
ExternalCheckVerdict = Literal["passed", "failed", "error", "unavailable"]


@dataclass(frozen=True)
class ExternalCheckItem:
    """One secret-free platform/infrastructure observation."""

    check_id: str
    label: str
    status: ExternalCheckStatus
    required: bool
    detail: str = ""
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "label": self.label,
            "status": self.status,
            "required": self.required,
            "detail": self.detail,
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True)
class ExternalCheckReport:
    """Platform check result kept deliberately outside Agent Evaluation."""

    platform: str
    bot_id: str
    verdict: ExternalCheckVerdict
    checks: tuple[ExternalCheckItem, ...]
    external_write_attempted: bool = False
    external_write_performed: bool = False
    limitations: tuple[str, ...] = ()
    schema: str = "external-platform-check/v1"
    scope: str = "external_platform"
    agent_evaluation: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "scope": self.scope,
            "platform": self.platform,
            "bot_id": self.bot_id,
            "verdict": self.verdict,
            "agent_evaluation": self.agent_evaluation,
            "external_write_attempted": self.external_write_attempted,
            "external_write_performed": self.external_write_performed,
            "checks": [item.to_dict() for item in self.checks],
            "limitations": list(self.limitations),
        }


# ---------------------------------------------------------------------------
# Platform adapter
# ---------------------------------------------------------------------------
class PlatformAdapter(abc.ABC):
    """单个渠道平台的统一适配器契约。

    子类必须以类属性声明 ``name`` / ``adapter_id`` 与三个能力位，并实现
    persona、文件回传、cc-connect 渲染等抽象方法。后台通知 / 身份补全 / 额外
    配置文件等带默认实现，平台按需覆盖。
    """

    #: BotSpec ``platform.type``，registry 的主键（如 ``feishu``）。
    name: str
    #: BotSpec ``platform.adapter`` 元数据标识（如 ``feishu_acp``）。
    adapter_id: str

    #: 是否启用 Owner/Admin/User 角色矩阵 + 业务模式切换工具。
    supports_role_matrix: bool = False
    #: 是否启用 “私聊文件上传 → per-user 私人空间” 附件流水线。
    supports_user_files_pipeline: bool = False
    #: 是否支持后台任务完成后的主动通知（依赖平台主动推送通道）。
    supports_background_jobs: bool = False
    #: 是否允许 Owner/Admin 角色按显示名兜底匹配。稳定 ID 平台应关闭。
    allow_role_name_match: bool = True
    #: 群会话作用域。``actor`` 保持 chat × user；``chat`` 由群 ID 共享。
    group_conversation_scope: str = "actor"
    #: 共享群会话必须由 cc-connect 为每条消息注入与正文绑定的 sender envelope。
    requires_sender_envelope: bool = False

    # Prompt assembly lives in middleware/application composition.

    # -- runtime: file delivery --------------------------------------------
    @abc.abstractmethod
    def resolve_sendable_paths(self, workspace: "Workspace", files: Sequence[str]) -> list[Path]:
        """把入参文件名/路径规范化成当前工作区内的可发送绝对路径集合。"""

    @abc.abstractmethod
    def send_files(
        self,
        files: Sequence[Path],
        *,
        message: str = "",
    ) -> str:
        """把工作区文件回传到当前会话；返回下游通道的 stdout/状态摘要。"""

    def send_workspace_files(
        self,
        workspace: "Workspace",
        files: Sequence[Path],
        *,
        message: str = "",
    ) -> str:
        """回传已绑定 workspace 的文件；默认兼容既有 adapter 契约。"""

        return self.send_files(files, message=message)

    # -- runtime: background notification ----------------------------------
    def resolve_delivery_target(self, workspace: "Workspace") -> Any:
        """选择后台任务通知的接收目标；不支持的平台抛 ``NotImplementedError``。"""
        raise NotImplementedError(f"platform={self.name!r} 不支持后台任务通知目标解析")

    def send_text(self, workspace: "Workspace", text: str, *, timeout: int = 10) -> Any:
        """把文本通知主动发送到当前会话；不支持的平台抛 ``NotImplementedError``。"""
        raise NotImplementedError(f"platform={self.name!r} 不支持后台任务主动通知")

    # -- runtime: identity --------------------------------------------------
    def resolve_user_display_name(self, user_id: str | None) -> str | None:
        """按平台用户标识回查显示名；默认无能力返回 ``None``。"""
        return None

    def parse_session_identity(
        self,
        *,
        session_key: str,
        hook_user_id: str | None = None,
        hook_chat_id: str | None = None,
        hook_chat_kind: str | None = None,
        hook_user_name: str | None = None,
    ) -> SessionIdentity:
        """Parse cc-connect session metadata into platform-neutral identity fields.

        The default parser handles the common ``platform:chat_id:user_id`` shape.
        Platform-specific adapters should override this when the session key shape
        differs, while still honoring explicit ``CC_HOOK_*`` values first.
        """
        user_id = (hook_user_id or "").strip()
        chat_id = (hook_chat_id or "").strip()
        chat_kind = (hook_chat_kind or "").strip()
        user_name = (hook_user_name or "").strip()

        if (not user_id or not chat_id) and session_key:
            parts = session_key.split(":", 2)
            if len(parts) >= 3:
                parsed_chat_id = parts[1].strip()
                parsed_user_id = parts[2].strip()
                if not chat_id:
                    chat_id = parsed_chat_id
                if not user_id:
                    user_id = parsed_user_id

        if not chat_kind:
            chat_kind = "p2p"

        return SessionIdentity(
            user_id=user_id or None,
            chat_id=chat_id or None,
            chat_kind=chat_kind or None,
            user_name=user_name or None,
        )

    # -- runtime: access gate ----------------------------------------------
    def detect_self_mention(
        self,
        text: str,
        *,
        env: Mapping[str, str],
        mention_name: str | None = None,
    ) -> bool | None:
        """判断这条消息文本是否 @ 了本机器人。

        供中间件的群聊 @门禁使用。返回值语义：

        - ``True`` / ``False``：明确判定被 @ / 未被 @。
        - ``None``：当前平台无法判定（如缺少识别本机器人所需的配置）。门禁遇到
          ``None`` 时按"配置缺失"处理（放行 + 告警），而非误杀。

        默认无能力返回 ``None``；需要群聊 @门禁的平台覆盖本方法。
        """
        return None

    # -- deploy -------------------------------------------------------------
    def required_secrets(self) -> tuple[SecretSpec, ...]:
        """声明该平台运行所需的 env 凭据；CLI / 部署脚本据此做前置校验。"""
        return ()

    def validate_runtime_env(self, env: Mapping[str, str]) -> tuple[str, ...]:
        """校验平台运行配置；错误消息不得包含 secret 原文。"""
        return ()

    def setup_actions(self) -> tuple[SetupActionSpec, ...]:
        """Optional platform setup actions for deployment consoles."""
        return ()

    def run_external_checks(
        self,
        env: Mapping[str, str],
        *,
        bot_id: str,
        send_message: bool = False,
        confirm_external_write: bool = False,
    ) -> ExternalCheckReport:
        """Run platform checks without invoking an Agent or Evaluation."""

        del env, send_message, confirm_external_write
        return ExternalCheckReport(
            platform=self.name,
            bot_id=bot_id,
            verdict="unavailable",
            checks=(
                ExternalCheckItem(
                    check_id="platform_external_check",
                    label="平台外部检查",
                    status="not_configured",
                    required=False,
                    detail=f"platform={self.name} 未配置外部检查",
                ),
            ),
            limitations=("该平台尚未提供外部检查实现。",),
        )

    @abc.abstractmethod
    def render_cc_connect_section(self, env: Mapping[str, str]) -> str:
        """渲染 cc-connect ``[[projects.platforms]]`` 配置片段（含末尾空行）。"""

    def render_extra_files(self, env: Mapping[str, str], home: Path) -> dict[str, str]:
        """渲染平台附带的额外配置文件，返回 ``{绝对路径: 文件内容}``。

        例如飞书需要 ``<home>/.lark-cli/config.json``。默认无额外文件。
        """
        return {}


__all__ = [
    "InboundMessage",
    "ExternalCheckItem",
    "ExternalCheckReport",
    "ExternalCheckStatus",
    "ExternalCheckVerdict",
    "OutboundMessage",
    "PlatformAdapter",
    "SecretSpec",
    "SessionIdentity",
    "SetupActionSpec",
]
