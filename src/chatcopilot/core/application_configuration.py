"""Application and Agent documents, resolved without changing process settings."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from chatcopilot.contracts.application import AgentProfile, ApplicationSpec


AGENT_CONFIGURATION_FIELDS = frozenset({"llm", "prompts", "tools", "agents", "context"})


def read_configuration_yaml(path: Path) -> dict[str, Any]:
    import yaml

    try:
        with path.open(encoding="utf-8") as stream:
            data = yaml.safe_load(stream)
    except yaml.YAMLError:
        raise ValueError(f"Configuration is not valid YAML: {path.name}") from None
    if not isinstance(data, dict):
        raise ValueError(f"Configuration must be a YAML mapping: {path}")
    if any(not isinstance(key, str) for key in data):
        raise ValueError("Configuration field names must be strings")
    return data


def agent_profile_path(application_path: Path, declaration: dict[str, Any]) -> Path:
    reference = declaration.get("agent_profile")
    if not isinstance(reference, str) or not reference.strip():
        raise ValueError(
            "应用配置需要 agent_profile 文件引用；旧内联配置请先运行 "
            "agentstrata botspec migrate-profile <bot.yaml>"
        )
    relative = Path(reference)
    root = application_path.parent.resolve()
    if relative.is_absolute() or ".." in relative.parts or "\\" in reference:
        raise ValueError("agent_profile 必须是应用目录内的相对路径")
    path = root / relative
    if not path.resolve().is_relative_to(root) or path.resolve() == application_path.resolve():
        raise ValueError("agent_profile 必须引用应用目录内独立的配置文件")
    if path.is_symlink():
        raise ValueError("agent_profile 必须是普通非符号链接文件")
    return path


def load_application_spec(path: str | Path) -> ApplicationSpec:
    source = Path(path).expanduser().resolve()
    data = read_configuration_yaml(source)
    inline = sorted(AGENT_CONFIGURATION_FIELDS & data.keys())
    if inline:
        raise ValueError(
            "Agent 配置必须移入 agent_profile，不能在应用配置中内联："
            + ", ".join(inline)
            + "；请运行 agentstrata botspec migrate-profile <bot.yaml>"
        )
    profile_path = agent_profile_path(source, data)
    profile = read_configuration_yaml(profile_path)
    unknown = sorted(profile.keys() - AGENT_CONFIGURATION_FIELDS)
    if unknown:
        raise ValueError("AgentProfile 包含应用或未知字段：" + ", ".join(map(str, unknown)))
    return ApplicationSpec(source, AgentProfile(profile_path, profile), data)


def split_configuration(data: dict[str, Any], *, profile: str = "agent.yaml") -> tuple[dict, dict]:
    """Split a complete assembly declaration for scaffolding or explicit migration."""
    if "agent_profile" in data:
        raise ValueError("配置已经包含 agent_profile；不能再次拆分")
    application = deepcopy({key: value for key, value in data.items() if key not in AGENT_CONFIGURATION_FIELDS})
    agent = deepcopy({key: value for key, value in data.items() if key in AGENT_CONFIGURATION_FIELDS})
    application["agent_profile"] = profile
    return application, agent
