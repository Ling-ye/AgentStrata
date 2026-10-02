"""BotSpec model.

A BotSpec describes one deployable specialized bot, such as a Feishu meeting
reminder bot or a QQ game guide bot.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from chatcopilot.contracts.subagents import (
    CachePolicySpec as CachePolicySpec,
    ContextPolicySpec as ContextPolicySpec,
    CustomSubagentSpec as CustomSubagentSpec,
    SearchProviderSpec as SearchProviderSpec,
    SearchLimitsSpec as SearchLimitsSpec,
    SubagentBudgetSpec as SubagentBudgetSpec,
    SubagentSpec as SubagentSpec,
    ToolSelectorSpec as ToolSelectorSpec,
)


@dataclass(frozen=True)
class ModelSpec:
    binding: str = "chat"


@dataclass(frozen=True)
class ValidationIssue:
    level: str
    message: str
    field: str = ""


@dataclass(frozen=True)
class PlatformSpec:
    type: str
    adapter: str


@dataclass(frozen=True)
class GatewaySpec:
    """Per-Bot loopback Gateway configuration by environment reference."""

    protocol_version: int = 1
    host: str = "127.0.0.1"
    port_env: str = "CHATCOPILOT_GATEWAY_PORT"
    token_env: str = "CHATCOPILOT_GATEWAY_TOKEN"
    state_root_env: str = "CHATCOPILOT_GATEWAY_STATE_ROOT"
    max_concurrent_turns: int = 8
    max_pending_ingress: int = 1024


@dataclass(frozen=True)
class QQChannelSpec:
    """Personal QQ Channel backed by an external OneBot v11 provider."""

    type: str = "qq_personal"
    provider: str = "onebot_v11"
    channel_id: str = "qq"
    endpoint_env: str = "CHATCOPILOT_QQ_ONEBOT_WS_URL"
    access_token_env: str = "QQ_ACCESS_TOKEN"
    account_env: str = "QQ_ACCOUNT"
    mention_only_groups: bool = True
    action_timeout_seconds: float = 120.0
    max_frame_bytes: int = 4 * 1024 * 1024


@dataclass(frozen=True)
class WeixinChannelSpec:
    """Personal ClawBot channel configured by private credential references."""

    type: str = "weixin_clawbot"
    provider: str = "ilink"
    channel_id: str = "weixin"
    access_token_env: str = "WEIXIN_BOT_TOKEN"
    account_env: str = "WEIXIN_BOT_ID"
    user_env: str = "WEIXIN_USER_ID"
    endpoint_env: str = "WEIXIN_API_BASE_URL"
    action_timeout_seconds: float = 120.0
    max_frame_bytes: int = 4 * 1024 * 1024


@dataclass(frozen=True)
class ChannelsSpec:
    """Transport Channels owned by the Bot's Gateway."""

    qq: QQChannelSpec | None = None
    weixin: WeixinChannelSpec | None = None


@dataclass(frozen=True)
class CodeLLMSpec:
    """Worker model binding and host execution settings."""
    enabled: bool = False
    env_prefix: str | None = None
    binding: str = "code"
    command: str = "codex exec --model {model} --cd {workdir}"
    timeout_seconds: int = 900


@dataclass(frozen=True)
class LLMSpec:
    """Versioned chat, research, and code model slots."""

    env_prefix: str = "CHATCOPILOT_CHAT"
    chat: ModelSpec = field(default_factory=ModelSpec)
    research: ModelSpec = field(default_factory=lambda: ModelSpec("research"))
    code: CodeLLMSpec = field(default_factory=CodeLLMSpec)


@dataclass(frozen=True)
class McpSpec:
    servers: str | None = None


@dataclass(frozen=True)
class RagSpec:
    sources: str | None = None


@dataclass(frozen=True)
class WikiSpec:
    """Writable private Wiki configuration declared under ``context.wiki``."""

    enabled: bool = False
    root_env: str = "CHATCOPILOT_WIKI_ROOT"
    label: str = "wiki"
    read_role: str = "owner"
    private_chat_only: bool = True
    max_chunk_chars: int = 1200


@dataclass(frozen=True)
class CodebaseSpec:
    registry: str | None = None


@dataclass(frozen=True)
class WorkspaceSpec:
    root_env: str = "CHATCOPILOT_WORKSPACE_ROOT"


@dataclass(frozen=True)
class DeploySpec:
    target: str = "wsl"
    instance_id: str | None = None
    wsl_home: str | None = None
    workspace_root: str | None = None
    log_dir: str | None = None
    env_file: str | None = None
    cc_connect_config_dir: str | None = None
    project_name: str | None = None
    secret_json: str | None = None


@dataclass(frozen=True)
class PackagingSpec:
    allowlist: str | None = None


@dataclass(frozen=True)
class SkillsSpec:
    """Skill 索引清单声明（manifest 指向 YAML 文件，文件列举本机器人启用的 skill）。"""

    manifest: str | None = None


@dataclass(frozen=True)
class PromptSpec:
    """Bot-authored presentation files; runtime policy is not configurable here."""

    schema_version: int
    identity: str
    response_style: str
    refusal_style: str | None = None
    role_styles: dict[str, str] = field(default_factory=dict)
    mode_styles: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolSpec:
    """Tools surface selected by a bot instance."""

    packs: tuple[str, ...] = ()
    mcp: McpSpec = field(default_factory=McpSpec)
    features: tuple[str, ...] = ()
    hide: tuple[str, ...] = ()


@dataclass(frozen=True)
class DevShellSpec:
    """Shell execution constraints for dev tools."""

    timeout_default: int = 60
    timeout_max: int = 300


@dataclass(frozen=True)
class DevSpec:
    """Dev tools configuration declared in ``context.dev``.

    ``root_env`` names the environment variable holding the project root path;
    the actual path value lives in ``local.env``, never in YAML.
    The host binds this configured project to Owner execution resources.
    """

    root_env: str = "CHATCOPILOT_DEV_ROOT"
    shell: DevShellSpec = field(default_factory=DevShellSpec)


@dataclass(frozen=True)
class ContextSpec:
    """Knowledge and material sources available to a bot."""

    rag: RagSpec = field(default_factory=RagSpec)
    wiki: WikiSpec = field(default_factory=WikiSpec)
    codebases: CodebaseSpec = field(default_factory=CodebaseSpec)
    playbooks: SkillsSpec = field(default_factory=SkillsSpec)
    dev: DevSpec = field(default_factory=DevSpec)


@dataclass(frozen=True)
class BotSpec:
    id: str
    display_name: str
    platform: PlatformSpec
    prompts: PromptSpec
    source_path: Path
    gateway: GatewaySpec | None = None
    channels: ChannelsSpec = field(default_factory=ChannelsSpec)
    llm: LLMSpec = field(default_factory=LLMSpec)
    tools: ToolSpec = field(default_factory=ToolSpec)
    agents: SubagentSpec = field(default_factory=SubagentSpec)
    context: ContextSpec = field(default_factory=ContextSpec)
    workspace: WorkspaceSpec = field(default_factory=WorkspaceSpec)
    deploy: DeploySpec = field(default_factory=DeploySpec)
    packaging: PackagingSpec = field(default_factory=PackagingSpec)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def base_dir(self) -> Path:
        return self.source_path.parent

    def resolve_path(self, value: str | None) -> Path | None:
        if not value:
            return None
        path = Path(value)
        if path.is_absolute():
            return path
        return self.base_dir / path
