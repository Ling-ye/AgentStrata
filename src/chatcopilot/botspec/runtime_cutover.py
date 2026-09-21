"""Phase-A declaration conversion used only by the explicit runtime cutover."""

from __future__ import annotations
import copy
import json
from pathlib import Path
from uuid import uuid4


def migration_model_snapshot(raw, environment):
    """Resolve helpers with the execution parsers, without clients or secrets."""
    from chatcopilot.botspec.loader import _parse_model_spec, _parse_subagents
    from chatcopilot.core.config import load_config, load_llm_profile
    from chatcopilot.core.model_routes import resolve_model_config

    llm = raw.get("llm", {})
    chat, research = llm.get("chat", {}), llm.get("research", {})
    prefix = chat.get("env_prefix", "CHATCOPILOT_CHAT")
    primary = resolve_model_config(
        _parse_model_spec(chat),
        fallback=load_config(env_prefix=prefix, environment=environment).llm,
        prefix=prefix,
        environment=environment,
    )
    helper = (
        load_config(env_prefix=research["inherit_env_prefix"], environment=environment).llm
        if research.get("inherit_env_prefix")
        else primary
    )
    research_model = resolve_model_config(
        _parse_model_spec(research),
        fallback=primary,
        prefix=research.get("env_prefix"),
        environment=environment,
    )
    agents = dict(raw.get("agents", {}))
    if "backend" in agents:
        agents["runtime"] = agents.pop("backend")
    parsed = _parse_subagents(agents, research_env_prefix=research.get("env_prefix"))

    def overlay(prefix, fallback):
        return (
            load_llm_profile(prefix, fallback=fallback, environment=environment)
            if prefix
            else fallback
        )

    def subagent_model(prefix):
        return overlay(prefix, helper) if prefix else primary

    auxiliary = {
        "research": research_model,
        "search": overlay(parsed.research_budget.model_env_prefix, research_model),
        "mcp_search": subagent_model(parsed.search_budget.model_env_prefix),
    }
    auxiliary.update(
        {
            "subagent:" + name: subagent_model(budget.model_env_prefix)
            for name, budget in parsed.agents.items()
        }
    )
    auxiliary.update(
        {
            "subagent:" + item.name: subagent_model(item.budget.model_env_prefix)
            for item in parsed.custom
        }
    )
    return {
        "main": primary.model_route().to_payload(),
        "auxiliary": {
            name: config.model_route().to_payload() for name, config in sorted(auxiliary.items())
        },
    }


def migrate_declaration(raw: dict, *, environment=None) -> dict:
    environment = {} if environment is None else environment
    value = copy.deepcopy(raw)
    agents = value.setdefault("agents", {})
    if "runtime" in agents and "backend" in agents:
        raise ValueError("mixed runtime declarations cannot be migrated")
    runtime = agents.pop("backend", agents.get("runtime", "native"))
    agents["runtime"] = runtime
    llm = value.setdefault("llm", {})
    chat = llm.setdefault("chat", {})
    if runtime == "codex" and "auth" not in chat:
        from chatcopilot.botspec.loader import _parse_subagents

        before = migration_model_snapshot(value, environment)["auxiliary"]
        effective_budgets = _parse_subagents(
            agents, research_env_prefix=llm.get("research", {}).get("env_prefix")
        )
        prefix = chat.get("env_prefix", "CHATCOPILOT_CHAT")
        code = llm.get("code", {})
        main_model = environment.get(prefix + "_CODE_MODEL") or code.get("model", "gpt-5.5")
        main_effort = environment.get(prefix + "_CODE_REASONING_EFFORT") or code.get(
            "reasoning_effort", "medium"
        )
        profiles = (
            json.loads(environment[prefix + "_CODE_PROFILES_JSON"])
            if environment.get(prefix + "_CODE_PROFILES_JSON")
            else code.get("profiles", {})
        )
        llm["chat"] = {
            "env_prefix": prefix + "_MAIN",
            "provider": "openai",
            "model": main_model,
            "reasoning_effort": main_effort,
            "auth": {"mode": "chatgpt", "profile": "main"},
            **({"profiles": copy.deepcopy(profiles)} if profiles else {}),
        }
        research = llm.setdefault("research", {})
        research["inherit_env_prefix"] = prefix
        # Freeze each effective budget before changing the shared fallback.
        # Search inherits research; ordinary subagents inherit old chat.
        agents.setdefault("defaults", {})["model_env_prefix"] = (
            effective_budgets.defaults.model_env_prefix or prefix
        )
        router = agents.setdefault("unified_search", agents.pop("research_router", {}))
        router["model_env_prefix"] = effective_budgets.research_budget.model_env_prefix
        agents.setdefault("search_budget", {})["model_env_prefix"] = (
            effective_budgets.search_budget.model_env_prefix or prefix
        )
        for name, budget in effective_budgets.agents.items():
            agents.setdefault(name, {})["model_env_prefix"] = budget.model_env_prefix or prefix
        for declaration, resolved in zip(agents.get("custom", []), effective_budgets.custom):
            declaration.setdefault("budget", {})["model_env_prefix"] = (
                resolved.budget.model_env_prefix or prefix
            )
        llm.setdefault("code", {})["env_prefix"] = prefix
        agents.setdefault("runtime_options", {})["native"] = {"env_prefix": prefix}
        agents.setdefault("runtime_options", {})["codex"] = {
            "turn_timeout_seconds": int(
                environment.get(prefix + "_CODE_TIMEOUT_SECONDS")
                or code.get("timeout_seconds", 900)
            )
        }
        after = migration_model_snapshot(value, environment)["auxiliary"]
        if before != after:
            changed = sorted(name for name in before if before[name] != after.get(name))
            raise ValueError(
                "auxiliary model migration changed effective routes: " + ", ".join(changed)
            )
    return value


def binding_targets(root: Path) -> tuple[Path, ...]:
    if (
        not root.is_absolute()
        or root.resolve() != root
        or root in {Path(root.anchor), Path.home().resolve()}
    ):
        raise ValueError("invalid workspace root")
    directories = [
        root / ".conversation-state" / "runtime-sessions",
        *root.glob("group_*/.conversation-state/runtime-sessions"),
    ]
    targets = [path for directory in directories for path in directory.glob("*/*.session.json")]
    selected = []
    for target in targets:
        if not any(name in {"runtime-sessions", ".runtime-sessions"} for name in target.parts):
            continue
        if target.is_symlink() or target.resolve() != target:
            raise ValueError("unsafe backend binding")
        value = json.loads(target.read_text())
        if value.get("schema_version") not in {2, 3}:
            continue
        selected.append(target)
    return tuple(selected)


def archive_bindings(root: Path) -> int:
    targets = binding_targets(root)
    for target in targets:
        target.rename(target.with_name(target.name + ".archived-" + uuid4().hex))
    return len(targets)
