"""Product configuration variation with fixed identity, model and resource authority."""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

_TUNABLE_NUMBERS = frozenset({"max_chunk_chars", "max_model_turns", "max_tool_calls", "timeout_seconds", "max_output_chars", "max_results"})


def configuration_invariants(raw: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(raw)
    prompts = result.get("prompts", {})
    result["prompts"] = {key: value for key, value in prompts.items() if key not in
                         {"identity", "response_style", "refusal_style", "role_styles", "mode_styles"}}
    tools = result.get("tools", {})
    for name in ("packs", "features", "exclude"):
        tools.pop(name, None)

    def fixed(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: fixed(item) for key, item in value.items()
                    if not (key in _TUNABLE_NUMBERS and type(item) in {int, float})}
        if isinstance(value, list):
            return [fixed(item) for item in value]
        return value
    for name in ("tools", "agents", "context"):
        if name in result:
            result[name] = fixed(result[name])
    return result


def configuration_path(name: str, bot_id: str | None = None) -> bool:
    parts = Path(name).parts
    return (len(parts) >= 3 and parts[0] == "bots" and (bot_id is None or parts[1] == bot_id)
            and (parts[2:] == ("bot.yaml",) or (parts[2] == "prompts" and name.endswith(".md"))))


def validate_configuration(before: bytes, after: bytes) -> None:
    original, candidate = yaml.safe_load(before), yaml.safe_load(after)
    if not isinstance(original, dict) or not isinstance(candidate, dict):
        raise ValueError("BotSpec must be a mapping")
    if configuration_invariants(original) != configuration_invariants(candidate):
        raise ValueError("candidate changes model, identity, resource authority or a non-tunable declaration")
    def paths(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key != "schema_version":
                    paths(item)
        elif value:
            if not isinstance(value, str):
                raise ValueError("prompt references must be relative Markdown paths")
            path = Path(value)
            if path.is_absolute() or ".." in path.parts or not path.parts or path.parts[0] != "prompts" or path.suffix != ".md":
                raise ValueError("candidate prompt reference escapes the Bot prompt directory")
    paths(candidate.get("prompts", {}))
