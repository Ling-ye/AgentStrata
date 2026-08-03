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
from chatcopilot.botspec.model import AccessSpec, BotSpec, CustomSubagentSpec, SubagentSpec
from chatcopilot.botspec.mcp import McpServerConfig, load_mcp_server_configs
from chatcopilot.botspec.rag import RagSourceConfig, load_rag_source_configs
from chatcopilot.botspec.registry import load_tool_pack_prompt, resolve_bot_spec_path
from chatcopilot.botspec.skills import SkillIndexEntry, load_skill_index
from chatcopilot.core.errors import RuntimeAssemblyError
from chatcopilot.core.settings import get_bot_spec_env
from chatcopilot.project import ENV_PREFIX


@dataclass(frozen=True)
class BotRuntimeContext:
    """Fully resolved runtime inputs for one deployable bot instance."""

    spec: BotSpec
    bot_id: str
    instance_id: str
    display_name: str
    platform_type: str
    platform_adapter: str
    system_prompt: str
    refusal_prompt: str | None
    safety_prompt_override: str | None
    memory_prompt_override: str | None
    mode_prompt_overrides: dict[str, str]
    role_prompt_overrides: dict[str, str]
    capability_prompt_fragments: tuple[str, ...]
    tool_packs: tuple[str, ...]
    tool_features: tuple[str, ...]
    exclude_tools: tuple[str, ...]
    memory_namespace: str
    workspace_root: str | None
    log_dir: str | None
    source_path: Path
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

    system_prompt = _read_required_text(spec, spec.prompts.persona, "prompts.persona")
    refusal_prompt = _read_optional_text(spec, spec.prompts.refusal)
    safety_prompt_override = _read_optional_text(spec, spec.prompts.safety)
    memory_prompt_override = _read_optional_text(spec, spec.prompts.memory_rules)
    mode_prompt_overrides = _read_prompt_map(spec, spec.prompts.modes)
    role_prompt_overrides = _read_prompt_map(spec, spec.prompts.roles)
    capability_prompt_fragments = _load_tool_pack_prompt_fragments(spec.tools.packs)
    skills = _load_skills(spec)
    instance_id = spec.deploy.instance_id or spec.id
    return BotRuntimeContext(
        spec=spec,
        bot_id=spec.id,
        instance_id=instance_id,
        display_name=spec.display_name,
        platform_type=spec.platform.type,
        platform_adapter=spec.platform.adapter,
        system_prompt=system_prompt,
        refusal_prompt=refusal_prompt,
        safety_prompt_override=safety_prompt_override,
        memory_prompt_override=memory_prompt_override,
        mode_prompt_overrides=mode_prompt_overrides,
        role_prompt_overrides=role_prompt_overrides,
        capability_prompt_fragments=capability_prompt_fragments,
        tool_packs=spec.tools.packs,
        tool_features=spec.tools.features,
        exclude_tools=spec.tools.hide,
        memory_namespace=spec.context.memory_store.namespace or spec.id,
        workspace_root=spec.deploy.workspace_root,
        log_dir=spec.deploy.log_dir,
        source_path=spec.source_path,
        agent_backend=spec.agents.backend,
        mcp_servers=load_mcp_server_configs(spec),
        rag_sources=load_rag_source_configs(spec),
        access=spec.access,
        skills=skills,
        subagents=_resolve_subagents(spec),
    )


def _resolve_subagents(spec: BotSpec) -> SubagentSpec:
    """Resolve custom subagent prompt pointers into inline ``system_prompt`` text."""
    if not spec.agents.custom and not spec.agents.overrides:
        return spec.agents
    resolved: list[CustomSubagentSpec] = []
    for custom in spec.agents.custom:
        prompt_text = _read_required_text(
            spec, custom.prompt_path, f"agents.custom.{custom.name}.prompt"
        )
        resolved.append(replace(custom, system_prompt=prompt_text))
    overrides: dict[str, CustomSubagentSpec] = {}
    for name, override in spec.agents.overrides.items():
        prompt_text = override.system_prompt
        if override.prompt_path:
            prompt_text = _read_required_text(spec, override.prompt_path, f"agents.{name}.prompt")
        overrides[name] = replace(override, system_prompt=prompt_text)
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


def _load_tool_pack_prompt_fragments(tool_pack_names: tuple[str, ...]) -> tuple[str, ...]:
    fragments: list[str] = []
    seen: set[str] = set()
    for name in tool_pack_names:
        pack = load_tool_pack_prompt(name)
        if pack is None:
            continue
        for fragment in pack.prompt_fragments:
            text = str(fragment).strip()
            if not text or text in seen:
                continue
            seen.add(text)
            fragments.append(text)
    return tuple(fragments)
