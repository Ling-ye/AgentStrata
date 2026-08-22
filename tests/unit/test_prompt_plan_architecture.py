from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from chatcopilot.agent.context.prompt_plan import (
    PromptBuildInput,
    PromptPlanBuilder,
    render_codex_prompt,
)
from chatcopilot.agent.tools.registry import discover_tools
from chatcopilot.botspec.runtime import load_runtime_context
from chatcopilot.contracts import Role, role_ge
from chatcopilot.contracts.tools import build_openai_schema


_ROOT = Path(__file__).resolve().parents[2]
_BOT = _ROOT / "bots" / "lingye-copilot-qq" / "bot.yaml"
_BASELINE = _ROOT / "tests" / "baselines" / "prompt_budget_v1.json"


def test_removed_prompt_modules_are_not_importable() -> None:
    removed = (
        "chatcopilot.agent.context.prompt_" + "builder",
        "chatcopilot.agent.subagents.prompt_" + "layers",
        "chatcopilot.agent." + "quality_gate",
        "chatcopilot.agent.persona." + "enrichment",
        "chatcopilot.middleware.acp.prompt_" + "assembler",
        "chatcopilot.middleware.acp.persistence_" + "receipt",
        "chatcopilot.platforms.qq." + "persona",
        "chatcopilot.platforms.feishu." + "persona",
    )
    for module in removed:
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(module)


def test_removed_contract_symbols_and_output_labels_do_not_reappear() -> None:
    banned = (
        "prompts." + "persona",
        "prompts." + "safety",
        "prompts." + "memory_rules",
        "legacy_" + "system_prompt",
        "legacy_" + "task",
        "PromptLayer" + "Spec",
        "capability_prompt_" + "fragments",
        "quality_gate_" + "level",
        "QUALITY_GATE_" + "LEVEL",
        "deterministic_persona_" + "directive",
        "build_system_" + "prompt",
        "system_" + "baseline",
        "session_dynamic_" + "tail",
        "system_" + "appendix",
        "[" + "KNOWN" + "]",
        "[" + "HIGH" + "]",
        "[" + "INFERRED" + "]",
        "[" + "COMPUTED" + "]",
        "[" + "COMMON" + "]",
        "[" + "FRAME" + "]",
        "[" + "GUESS" + "]",
    )
    roots = (_ROOT / "src", _ROOT / "bots", _ROOT / "docs", _ROOT / "specs")
    violations: list[str] = []
    for root in roots:
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix not in {
                ".py",
                ".md",
                ".yaml",
                ".template",
                ".example",
            }:
                continue
            text = path.read_text(encoding="utf-8")
            for token in banned:
                if token in text:
                    violations.append(f"{path.relative_to(_ROOT)}: {token}")
    assert violations == []


def test_prompt_builder_has_one_production_definition() -> None:
    definitions = []
    for path in (_ROOT / "src" / "chatcopilot").rglob("*.py"):
        if "class PromptPlan" + "Builder" in path.read_text(encoding="utf-8"):
            definitions.append(path.relative_to(_ROOT).as_posix())
    assert definitions == ["src/chatcopilot/agent/context/prompt_plan.py"]


def test_lingye_prompt_and_tool_schema_budgets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_MCP_AUTHORIZATION", "Bearer test-credential")
    runtime = load_runtime_context(_BOT)
    baseline = json.loads(_BASELINE.read_text(encoding="utf-8"))["profiles"]
    tools = discover_tools(
        tool_packs=runtime.tool_packs,
        exclude_tools=runtime.exclude_tools,
    )

    for role in ("owner", "user"):
        for channel in ("private", "group"):
            key = f"{role}_{channel}"
            plan = PromptPlanBuilder().build(
                PromptBuildInput(
                    profile=runtime.prompt_profile,
                    backend="codex",
                    model="gpt-5.6-terra",
                    role=role,
                    channel_kind=channel,
                    session_policy=(
                        "当前可信角色与会话作用域已由 transport attestation 和运行时完成校验。"
                    ),
                    capability_policies=runtime.capability_policies,
                    skill_index=runtime.skills,
                )
            )
            rendered = render_codex_prompt(plan, user_message="你是谁")
            assert len(rendered) == baseline[key]["prompt_chars"]
            assert len(rendered) <= baseline[key]["prompt_limit"]
            assert len(rendered) <= 6650
            ids = [layer.id for layer in plan.layers]
            assert len(ids) == len(set(ids))
            assert ids.count("capability.skills") == 1

            visible = []
            for tool in tools:
                if tool.requires_role is not None and not role_ge(Role(role), tool.requires_role):
                    continue
                if tool.metadata.get("private_chat_only") and channel != "private":
                    continue
                visible.append(build_openai_schema(tool))
            schema_text = json.dumps(
                sorted(visible, key=lambda item: item["function"]["name"]),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            assert len(schema_text) == baseline[key]["tool_schema_chars"]
            assert len(schema_text) <= baseline[key]["tool_schema_limit"]
            assert len(schema_text) * 100 <= (baseline[key]["historical_tool_schema_chars"] * 85)
