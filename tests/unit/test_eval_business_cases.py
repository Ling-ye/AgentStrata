from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
import json

import pytest

from chatcopilot.agent.tools.builtin.memory_tools import TOOLS as MEMORY_TOOLS
from chatcopilot.agent.tools.builtin.skill_tools import build_skill_provider
from chatcopilot.contracts.agent import AgentResult, ToolStarted, ToolFinished
from chatcopilot.contracts.tools import ToolContext
from chatcopilot.core.llm_client import ChatResult
from chatcopilot.evals import capability_executor as executor
from chatcopilot.evals.business_cases import BUSINESS_IDS, BusinessFixture, missing_requirements
from chatcopilot.evals.business_verifiers import business_checks
from chatcopilot.evals.manifest import load_case_definitions
from chatcopilot.evals.registry import get_manifest

SUITE = "agentstrata-capabilities-v1"


class DraftModel:
    model = "controlled-draft"

    def chat(self, **kwargs):
        return ChatResult(
            content=json.dumps(
                {"markdown": "# 园艺助手\n耐心解释园艺问题，使用简短中文。", "source_urls": []}
            ),
            tool_calls=[],
        )


class ScriptedSession:
    """Exercise actual host handlers; responses are explicitly deterministic test data."""

    def __init__(self, runtime, options):
        self.options = options
        self.runtime = runtime
        self.service = options["workspace_service"]
        providers = (*runtime.providers, *options.get("session_providers", ()))
        all_tools = [t for p in providers for ts in p.packs.values() for t in ts]
        all_tools += list(MEMORY_TOOLS)
        skill = build_skill_provider(runtime.bot.skills)
        all_tools += [t for ts in skill.packs.values() for t in ts]
        self.tools = {t.name: t for t in all_tools if options["permission_filter"](t) is None}
        self.capabilities = SimpleNamespace(tool_names=tuple(self.tools))
        self.plan = None

    def set_prompt_plan(self, plan):
        self.plan = plan

    def close(self):
        pass

    def run_task(self, task, on_event):
        ident = task.metadata["eval_case"]
        index = task.metadata["eval_turn"]
        context = ToolContext(
            workspace=self.service.resolve_workspace(),
            workspace_root=self.service.resolve_workspace_root(),
            persistent_state=self.service.resolve_persistent_state(),
            caller_role="owner",
            request_text=task.text,
        )

        def call(name, **args):
            on_event(ToolStarted(name, args, trace_id=f"{ident}-{index}-{name}"))
            result = self.tools[name].handler(args, context)
            on_event(
                ToolFinished(
                    name,
                    result.ok,
                    result.summary,
                    result.error,
                    data=result.to_llm_payload(),
                    trace_id=f"{ident}-{index}-{name}",
                )
            )
            return result

        text = "已核对。"
        if ident in {"decision-select-tool", "decision-similar-tools", "decision-retry"}:
            r = call("lookup_catalog", query="纸灯")
            if not r.ok:
                r = call("lookup_catalog", query="纸灯")
            text = str(r.data["stock"])
        elif ident == "decision-chain":
            r = call("lookup_catalog", query="纸灯")
            text = call("read_inventory_record", record_id=r.data["record_id"]).data["verification"]
        elif ident == "decision-persona":
            r = call("persona_manage", operation="set", scope="group", requirement=task.text)
            assert r.data.get("committed"), r
            text = "已保存园艺助手人格。"
        elif ident == "decision-write-failure":
            assert not call("save_report", content="本周已核对库存").ok
            text = "未保存：写入失败。"
        elif ident == "decision-no-tool":
            text = "你好"
        elif ident == "decision-clarify":
            text = "请提供商品名称。"
        elif ident == "context-correction":
            text = "5"
        elif ident == "context-constraints":
            text = "这是一盏蓝色纸灯。"
        elif ident == "context-topic-return":
            text = "园艺社，周六"
        elif ident.startswith("memory-"):
            if ident == "memory-explicit-only":
                if index == 1:
                    call("append_memory", text="我的长期偏好：解释时先给结论。")
            elif ident == "memory-new-session":
                if index == 0:
                    call("append_memory", text="我的长期偏好：先看表格再看解释。")
                else:
                    text = call("read_memory").data["text"]
            else:
                if index < 2:
                    call(
                        "append_memory",
                        text="我的长期偏好："
                        + ("简短回答。" if index == 0 else "更正为详细解释，旧偏好不再适用。"),
                    )
                else:
                    text = call("read_memory").data["text"]
        elif ident.startswith("evidence-"):
            call("search_reference", query="纸灯")
            text = {
                "evidence-select-source": "配送说明：3 天",
                "evidence-synthesis": "产品与配送资料：45 元，3 天",
                "evidence-conflict": "两份记录 17 和 19，不能确定。",
                "evidence-unknown": "未记录电池寿命，无法确定。",
                "evidence-freshness": "2026 年生效政策：14 天",
                "evidence-injection": "蓝色",
            }[ident]
        elif ident == "artifact-image-table":
            call("save_report", content="order_id\nAS-2048\n")
            text = "已保存。"
        elif ident == "artifact-document-report":
            call("read_source_document")
            call("save_report", content="净销售额 40 元。")
            text = "已保存。"
        elif ident == "artifact-invalid-document":
            assert not call("read_source_document").ok
            text = "文件损坏，无法统计。"
        elif ident == "artifact-image-conflict":
            text = "图片为 AS-2048，与 AS-9999 不同。"
        elif ident.startswith("delegate-"):
            # Controlled public nested events, not a claim of real model delegation.
            for name in ["consult_inventory"] + (
                [] if ident == "delegate-autonomous" else ["consult_shipping"]
            ):
                on_event(ToolStarted(name, {"task": task.text}, trace_id=name))
                on_event(
                    ToolFinished(
                        name,
                        True,
                        "结果",
                        data={
                            "ok": True,
                            "data": {
                                "summary": "SHIPPING_UNAVAILABLE"
                                if ident == "delegate-partial-failure" and name.endswith("shipping")
                                else "17 件，3 天"
                            },
                        },
                        trace_id=name,
                    )
                )
            from chatcopilot.contracts.agent import SpanStarted

            on_event(SpanStarted("subagent:inventory", "subagent"))
            text = (
                "17 件；配送服务不可用，时效未知。"
                if ident == "delegate-partial-failure"
                else "17 件，3 天。"
            )
        elif ident.startswith("skill-"):
            call(
                "read_bot_skill",
                skill_id="ai-career-intelligence" if ident == "skill-career" else "ai-jd-analysis",
            )
            text = (
                "依据给定材料，Python 匹配，工具和评测经历未知。"
                if ident != "skill-missing-input"
                else "请提供简历和目标岗位。"
            )
        return AgentResult(text, "end_turn")


class ScriptedRuntime:
    def __init__(self, bot, overrides):
        self.bot = bot
        self.providers = overrides.runtime_providers
        self.research_llm = DraftModel()
        self.sessions = []

    def new_session(self, **kwargs):
        session = ScriptedSession(self, kwargs)
        self.sessions.append(session)
        return session

    def close(self):
        pass


@pytest.fixture
def controlled_business(monkeypatch):
    real_load = executor.load_evaluation_runtime
    runtime = real_load(
        "lingye-copilot-qq", load_local_environment=False, inherit_environment=False
    )
    monkeypatch.setattr(executor, "load_evaluation_runtime", lambda *a, **kw: runtime)
    from chatcopilot.core.config import ChatConfig

    monkeypatch.setattr(executor, "load_config", lambda **kw: ChatConfig())
    monkeypatch.setattr(
        executor, "assemble_agent_runtime", lambda bot, **kw: ScriptedRuntime(bot, kw["overrides"])
    )
    return runtime


@pytest.mark.parametrize("case_id", sorted(BUSINESS_IDS))
def test_business_positive_and_missing_evidence(
    case_id, tmp_path, controlled_business, deepeval_judge
):
    from chatcopilot.evals.deepeval_engine import score

    definition = next(c for c in load_case_definitions(get_manifest(SUITE)) if c.case_id == case_id)
    resources, evidence = executor._stage_resources(SUITE, definition, tmp_path)
    observation = executor._execute_agent_definition(
        definition,
        suite_id=SUITE,
        bot="controlled",
        workspace_path=tmp_path,
        resources_by_id=resources,
        resource_evidence=evidence,
    )
    checks = business_checks(case_id, observation)
    assert all(checks.values()), checks
    result, details = score(definition, observation)
    assert result.passed, details
    assert details["judge_kind"] == "deepeval"
    assert any("evaluation_owned_resources" in prompt for prompt in deepeval_judge.prompts)
    absent = replace(
        observation,
        evidence=tuple(e for e in observation.evidence if e.get("kind") != "business_snapshot"),
    )
    assert not all(business_checks(case_id, absent).values())
    assert not score(definition, absent)[0].passed


def test_skill_preflight_does_not_install_missing_capability():
    assert missing_requirements("skill-career", SimpleNamespace(skills=())) == [
        "skill:ai-career-intelligence"
    ]


def test_data_flow_rejects_forged_id_and_save_rejects_symlink(tmp_path):
    fixture = BusinessFixture("decision-chain", tmp_path)
    tools = {t.name: t for t in fixture.tools()}
    assert not tools["read_inventory_record"].handler({"record_id": "fabricated"}, ToolContext()).ok
    other = tmp_path / "other"
    other.mkdir()
    fixture = BusinessFixture("artifact-document-report", other)
    protected = tmp_path / "protected.txt"
    protected.write_text("unchanged")
    (other / "report.txt").symlink_to(protected)
    save = next(t for t in fixture.tools() if t.name == "save_report")
    with pytest.raises(OSError):
        save.handler({"content": "replacement"}, ToolContext())
    assert protected.read_text() == "unchanged"

@pytest.mark.parametrize('backend', ['native', 'langgraph'])
def test_real_backend_two_model_turns_use_actual_tool_result(backend, tmp_path, monkeypatch):
    from chatcopilot.agent import runtime as agent_module
    from chatcopilot.core.config import ChatConfig
    from chatcopilot.core.llm_client import LLMClient
    runtime = executor.load_evaluation_runtime('lingye-copilot-qq', load_local_environment=False, inherit_environment=False)
    runtime = replace(runtime, agent_backend=backend)
    monkeypatch.setattr(executor, 'load_evaluation_runtime', lambda *a, **kw: runtime)
    monkeypatch.setattr(executor, 'load_config', lambda **kw: ChatConfig())
    calls = []
    class Model(LLMClient):
        def _build_client(self):
            return SimpleNamespace(close=lambda: None)
        def __init__(self, cfg):
            super().__init__(cfg)
        def chat(self, **kwargs):
            messages = kwargs['messages']
            calls.append(messages)
            if len(calls) == 1:
                return ChatResult(content='', tool_calls=[{'id': 'lookup-1', 'type': 'function', 'function': {'name': 'lookup_catalog', 'arguments': '{"query":"纸灯"}'}}])
            assert any('17' in str(m.get('content')) and m.get('role') == 'tool' for m in messages)
            return ChatResult(content='当前库存 17 件。')
    monkeypatch.setattr(agent_module, 'LLMClient', Model)
    definition = next(c for c in load_case_definitions(get_manifest(SUITE)) if c.case_id == 'decision-select-tool')
    observation = executor._execute_agent_definition(definition, suite_id=SUITE, bot='controlled', workspace_path=tmp_path, resources_by_id={}, resource_evidence=())
    assert len(calls) == 2
    assert all(business_checks(definition.case_id, observation).values())
