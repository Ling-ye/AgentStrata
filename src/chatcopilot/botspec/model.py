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
    SubagentBudgetSpec as SubagentBudgetSpec,
    SubagentSpec as SubagentSpec,
    ToolSelectorSpec as ToolSelectorSpec,
)
from chatcopilot.contracts.model_selection import CodeModelProfile


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


@dataclass(frozen=True)
class ChannelsSpec:
    """Transport Channels owned by the Bot's Gateway."""

    qq: QQChannelSpec | None = None


@dataclass(frozen=True)
class CodeLLMSpec:
    """Versioned non-secret policy for the Codex mutation route."""

    enabled: bool = False
    mode: str = "rules"
    prefixes: tuple[str, ...] = ("/code", "/codex", "用codex")
    chat_prefixes: tuple[str, ...] = ("/chat", "/deepseek", "/ds")
    provider: str = "codex_cli"
    model: str = "gpt-5.5"
    reasoning_effort: str = "medium"
    profiles: dict[str, CodeModelProfile] = field(default_factory=dict)
    code_task_profile: str | None = None
    command: str = "codex exec --model {model} --cd {workdir}"
    workdir_env: str = "CHATCOPILOT_DEV_ROOT"
    timeout_seconds: int = 900
    allowed_roles: tuple[str, ...] = ("owner", "admin")


@dataclass(frozen=True)
class LLMSpec:
    """Versioned chat, research, and code model slots."""

    env_prefix: str = "CHATCOPILOT_CHAT"
    research_env_prefix: str | None = None
    research_model: str | None = None
    research_execution: str = "agent"
    research_prefixes: tuple[str, ...] = ("/research", "/deep-research", "/调研")
    research_web_search: str = "live"
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
class MemorySpec:
    provider: str = "markdown"
    namespace: str | None = None
    schema: str | None = None


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
class AccessSpec:
    """Capability projection policy after a message has been admitted."""

    owner_only_project_access: bool = False



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
    ``allowed_paths`` / ``denied_paths`` are glob patterns injected into
    ``CHATCOPILOT_DEV_ALLOWED_PATHS`` / ``CHATCOPILOT_DEV_DENIED_PATHS`` at
    process startup so that ``path_guard`` enforces them.
    """

    root_env: str = "CHATCOPILOT_DEV_ROOT"
    allowed_paths: tuple[str, ...] = ()
    denied_paths: tuple[str, ...] = ()
    shell: DevShellSpec = field(default_factory=DevShellSpec)


@dataclass(frozen=True)
class ContextSpec:
    """Knowledge and material sources available to a bot."""

    rag: RagSpec = field(default_factory=RagSpec)
    wiki: WikiSpec = field(default_factory=WikiSpec)
    memory_store: MemorySpec = field(default_factory=MemorySpec)
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
    access: AccessSpec = field(default_factory=AccessSpec)
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
