"""BotSpec runtime assembly.

Resolves a parsed :class:`BotSpec` into a fully materialized
:class:`BotRuntimeContext` ready to be consumed by middleware and the agent
runtime.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path

from chatcopilot.botspec.loader import load_botspec, validate_botspec
from chatcopilot.botspec.model import (
    AccessSpec,
    BotSpec,
    ChannelsSpec,
    CustomSubagentSpec,
    GatewaySpec,
    SubagentSpec,
)
from chatcopilot.botspec.mcp import McpServerConfig, load_mcp_server_configs
from chatcopilot.botspec.rag import RagSourceConfig, load_rag_source_configs
from chatcopilot.botspec.registry import load_tool_pack_policies, resolve_bot_spec_path
from chatcopilot.botspec.skills import SkillIndexEntry, load_skill_index
from chatcopilot.core.errors import RuntimeAssemblyError
from chatcopilot.core.settings import get_bot_spec_env
from chatcopilot.project import ENV_PREFIX
from chatcopilot.contracts.prompt import BotPromptProfile
from chatcopilot.contracts.tool_packs import ToolPackPolicy


@dataclass(frozen=True)
class BotRuntimeContext:
    """Fully resolved runtime inputs for one deployable bot instance."""

    spec: BotSpec
    bot_id: str
    instance_id: str
    display_name: str
    platform_type: str
    platform_adapter: str
    prompt_profile: BotPromptProfile
    capability_policies: tuple[ToolPackPolicy, ...]
    tool_packs: tuple[str, ...]
    tool_features: tuple[str, ...]
    exclude_tools: tuple[str, ...]
    memory_namespace: str
    workspace_root: str | None
    log_dir: str | None
    source_path: Path
    gateway: GatewaySpec | None = None
    channels: ChannelsSpec = field(default_factory=ChannelsSpec)
    agent_backend: str = "native"
    mcp_servers: tuple[McpServerConfig, ...] = ()
    rag_sources: tuple[RagSourceConfig, ...] = ()
    access: AccessSpec = AccessSpec()
    skills: tuple[SkillIndexEntry, ...] = ()
    subagents: SubagentSpec = field(default_factory=SubagentSpec)


def load_runtime_context(path_or_id: str | Path | None = None) -> BotRuntimeContext:
    """Load and assemble the runtime context selected by CLI/env."""

    selected = path_or_id or get_bot_spec_env()
    if selected is None:
        selected = os.environ.get(f"{ENV_PREFIX}_BOT_ID")
    if selected is None:
        raise RuntimeAssemblyError(
            f"未指定 BotSpec；请传入 --bot，或设置 {ENV_PREFIX}_BOT_SPEC / {ENV_PREFIX}_BOT_ID"
        )
    path = resolve_bot_spec_path(selected)
    return assemble_runtime_context(load_botspec(path))


def assemble_runtime_context(spec: BotSpec) -> BotRuntimeContext:
    """Validate and resolve all BotSpec file references needed at runtime."""

    issues = validate_botspec(spec)
    errors = [issue for issue in issues if issue.level == "error"]
    if errors:
        detail = "; ".join(f"{issue.field}: {issue.message}" for issue in errors)
        raise RuntimeAssemblyError(f"BotSpec 校验失败: {detail}")

    prompt_profile = BotPromptProfile(
        identity=_read_required_text(spec, spec.prompts.identity, "prompts.identity"),
        response_style=_read_required_text(
            spec,
            spec.prompts.response_style,
            "prompts.response_style",
        ),
        refusal_style=_read_optional_text(spec, spec.prompts.refusal_style) or "",
        mode_styles=_read_prompt_map(spec, spec.prompts.mode_styles),
        role_styles=_read_prompt_map(spec, spec.prompts.role_styles),
    )
    capability_policies = _load_tool_pack_policies(spec.tools.packs)
    skills = _load_skills(spec)
    instance_id = spec.deploy.instance_id or spec.id
    return BotRuntimeContext(
        spec=spec,
        bot_id=spec.id,
        instance_id=instance_id,
        display_name=spec.display_name,
        platform_type=spec.platform.type,
        platform_adapter=spec.platform.adapter,
        prompt_profile=prompt_profile,
        capability_policies=capability_policies,
        tool_packs=spec.tools.packs,
        tool_features=spec.tools.features,
        exclude_tools=spec.tools.hide,
        memory_namespace=spec.context.memory_store.namespace or spec.id,
        workspace_root=spec.deploy.workspace_root,
        log_dir=spec.deploy.log_dir,
        source_path=spec.source_path,
        gateway=spec.gateway,
        channels=spec.channels,
        agent_backend=spec.agents.backend,
        mcp_servers=load_mcp_server_configs(spec),
        rag_sources=load_rag_source_configs(spec),
        access=spec.access,
        skills=skills,
        subagents=_resolve_subagents(spec),
    )


def _resolve_subagents(spec: BotSpec) -> SubagentSpec:
    """Resolve the single role-prompt pointer for each custom subagent."""
    if not spec.agents.custom and not spec.agents.overrides:
        return spec.agents
    resolved: list[CustomSubagentSpec] = []
    for custom in spec.agents.custom:
        prompt_text = _read_required_text(
            spec,
            custom.role_prompt_path,
            f"agents.custom.{custom.name}.prompt.role",
        )
        resolved.append(replace(custom, role_prompt=prompt_text))
    overrides: dict[str, CustomSubagentSpec] = {}
    for name, override in spec.agents.overrides.items():
        prompt_text = override.role_prompt
        if override.role_prompt_path:
            prompt_text = _read_required_text(
                spec,
                override.role_prompt_path,
                f"agents.{name}.prompt.role",
            )
        overrides[name] = replace(override, role_prompt=prompt_text)
    return replace(spec.agents, custom=tuple(resolved), overrides=overrides)


def _read_required_text(spec: BotSpec, value: str | None, field: str) -> str:
    path = spec.resolve_path(value)
    if path is None or not path.is_file():
        raise RuntimeAssemblyError(f"{field} 指向的文件不存在: {path}")
    return path.read_text(encoding="utf-8").strip()


def _read_optional_text(spec: BotSpec, value: str | None) -> str | None:
    path = spec.resolve_path(value)
    if path is None:
        return None
    if not path.is_file():
        raise RuntimeAssemblyError(f"可选 prompt 文件不存在: {path}")
    return path.read_text(encoding="utf-8").strip()


def _read_prompt_map(spec: BotSpec, mapping: dict[str, str]) -> dict[str, str]:
    """把 ``{key: 相对路径}`` 解析成 ``{key: 文件内容}``；空值返回空 dict。"""
    resolved: dict[str, str] = {}
    for key, value in mapping.items():
        text = _read_optional_text(spec, value)
        if text:
            resolved[key] = text
    return resolved


def _load_skills(spec: BotSpec) -> tuple[SkillIndexEntry, ...]:
    manifest_value = spec.context.playbooks.manifest
    if not manifest_value:
        return ()
    manifest_path = spec.resolve_path(manifest_value)
    if manifest_path is None or not manifest_path.is_file():
        return ()
    return load_skill_index(manifest_path)


def _load_tool_pack_policies(
    tool_pack_names: tuple[str, ...],
) -> tuple[ToolPackPolicy, ...]:
    policies: list[ToolPackPolicy] = []
    seen_ids: set[str] = set()
    for name in tool_pack_names:
        for policy in load_tool_pack_policies(name):
            if policy.id in seen_ids:
                raise RuntimeAssemblyError(f"duplicate tool pack policy id: {policy.id}")
            seen_ids.add(policy.id)
            policies.append(policy)
    return tuple(policies)
