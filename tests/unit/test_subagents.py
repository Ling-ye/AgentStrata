from __future__ import annotations

from dataclasses import replace
import json
from tempfile import TemporaryDirectory
import unittest
from unittest import mock
from pathlib import Path
from types import SimpleNamespace

from chatcopilot.agent.runtime import AgentRuntime
from chatcopilot.core.config import ChatConfig
from chatcopilot.core.llm_client import ChatResult
from chatcopilot.agent.search_policy import SEARCH_DOMAINS
from chatcopilot.agent.subagents.registry import (
    _build_search_prompt,
    _make_delegate_tool,
    build_subagent_tools,
)
from chatcopilot.agent.subagents.delegate_tools import _adapter_candidate_digest
from chatcopilot.agent.subagents.runner import (
    SubagentRuntimeConfig,
    _allowed_for_subagent,
)
from chatcopilot.agent.tools.executor import ToolExecutor
from chatcopilot.agent.subagents.spec import SubagentDef, ToolMatchRule, ToolSelectorSpec
from chatcopilot.botspec.loader import load_botspec, validate_botspec
from chatcopilot.botspec.mcp import McpServerConfig
from chatcopilot.botspec.model import CustomSubagentSpec, SubagentBudgetSpec, SubagentSpec
from chatcopilot.component_catalog.subagents import BUILTIN_SUBAGENTS
from chatcopilot.contracts.adapter_approval import AdapterApprovalEnvelope
from chatcopilot.contracts.agent_backend import (
    BackendCapabilities,
    BackendSessionRef,
    CAPABILITY_CHAT,
    CAPABILITY_TOOLS,
    CodexMainSessionPolicy,
)
from chatcopilot.core.adapter_approval import AdapterApprovalStore
from chatcopilot.external_tools.shared.tool_spec import ToolDef


class _FakeLLM:
    def __init__(self, result: ChatResult) -> None:
        self.result = result
        self.seen_tools: list[str] = []

    def chat(self, **kwargs):
        self.seen_tools = [
            str((entry.get("function") or {}).get("name") or "")
            for entry in kwargs.get("tools") or []
        ]
        return self.result


class _ScriptedLLM:
    """按调用次数依次返回多个 ChatResult，用于模拟先调工具再收尾。"""

    def __init__(self, results: list[ChatResult]) -> None:
        self._results = results
        self._idx = 0
        self.seen_tools: list[str] = []

    def chat(self, **kwargs):
        self.seen_tools = [
            str((entry.get("function") or {}).get("name") or "")
            for entry in kwargs.get("tools") or []
        ]
        result = self._results[min(self._idx, len(self._results) - 1)]
        self._idx += 1
        return result


def _submit_call(payload: dict) -> ChatResult:
    return ChatResult(
        content="",
        tool_calls=[
            {
                "id": "call_submit",
                "type": "function",
                "function": {"name": "submit_result", "arguments": json.dumps(payload)},
            }
        ],
        finish_reason="tool_calls",
    )


def _tool(name: str, *, category: str = "", owner: str = "", module: str = "") -> ToolDef:
    def _handler(args: dict):
        return (f"{name} done", [], None)

    return ToolDef(
        name=name,
        summary=f"{name} summary",
        properties={},
        required=[],
        handler=_handler,
        category=category,
        owner=owner,
        module=module,
    )


class SubagentTests(unittest.TestCase):
    def test_search_subagent_honors_mcp_search_only_tools(self) -> None:
        def _mcp_tool(remote_name: str) -> ToolDef:
            return ToolDef(
                name=f"web_{remote_name}",
                summary=f"{remote_name} summary",
                properties={},
                required=[],
                handler=lambda args: ("ok", [], None),
                category="mcp",
                owner="tavily",
                metadata={
                    "mcp_server_id": "tavily",
                    "mcp_exposure": "subagent",
                    "mcp_remote_name": remote_name,
                    "mcp_search_only_tools": ["tavily_search", "tavily_extract"],
                },
            )

        self.assertTrue(_allowed_for_subagent(_mcp_tool("tavily_search"), "search_tavily"))
        self.assertTrue(_allowed_for_subagent(_mcp_tool("tavily_extract"), "search_tavily"))
        self.assertFalse(_allowed_for_subagent(_mcp_tool("tavily_research"), "search_tavily"))

    def test_lingye_codex_declares_providers_without_search_subagents(self) -> None:
        spec = load_botspec(Path("bots/lingye-copilot-qq/bot.yaml"))
        errors = [issue for issue in validate_botspec(spec) if issue.level == "error"]

        self.assertEqual(errors, [])
        self.assertEqual(spec.agents.backend, "codex")
        self.assertEqual(spec.agents.include, ())
        self.assertEqual(spec.agents.agents, {})
        self.assertTrue(spec.agents.research_enabled)
        self.assertEqual(
            [provider.id for provider in spec.agents.search_providers],
            ["tavily", "brave", "searxng"],
        )
        self.assertEqual(spec.agents.codex.owner_access, "worktree")
        self.assertEqual(spec.agents.codex.member_access, "workspace")

    def test_search_subagent_is_generated_from_mcp_source(self) -> None:
        xhs = _tool("xhs_search_feeds", category="mcp", owner="xiaohongshu")
        xhs.metadata.update(
            {
                "mcp_server_id": "xiaohongshu",
                "mcp_exposure": "subagent",
                "mcp_allowed_subagents": ["search_xiaohongshu"],
                "mcp_risk": "search",
            }
        )
        tools = build_subagent_tools(
            session_id="sid",
            subagents=SubagentSpec(search_budget=SubagentBudgetSpec(max_model_turns=1, max_tool_calls=1)),
            main_llm=_FakeLLM(ChatResult(content="unused")),
            main_config=ChatConfig(),
            base_tools=(xhs,),
            mcp_configs=(
                McpServerConfig(
                    id="xiaohongshu",
                    risk="search",
                    search_summary="Search Xiaohongshu feeds.",
                    preferred_domains=("xiaohongshu.com",),
                    excluded_domains=("spam.example",),
                    search_domain_guidance="Use for first-hand consumer experiences.",
                ),
            ),
        )

        research_tool = next(tool for tool in tools if tool.name == "search_xiaohongshu")

        self.assertIn("Delegate search to xiaohongshu", research_tool.summary)
        self.assertIn("xiaohongshu", research_tool.summary)
        self.assertEqual(
            research_tool.required,
            [
                "objective",
                "domain",
                "target_sites",
                "time_window",
                "required_fields",
                "cross_check",
            ],
        )
        self.assertEqual(
            research_tool.properties["domain"]["enum"],
            list(SEARCH_DOMAINS),
        )
        self.assertNotIn("_required", research_tool.properties)

    def test_search_prompt_enforces_site_and_source_quality_rules(self) -> None:
        prompt = _build_search_prompt(
            McpServerConfig(
                id="xiaohongshu",
                risk="search",
                preferred_domains=("xiaohongshu.com",),
                excluded_domains=("spam.example",),
                search_domain_guidance="Use for first-hand consumer experiences.",
            )
        )

        self.assertIn("official/primary sources", prompt.task_focus)
        self.assertIn("site:hostname", prompt.task_focus)
        self.assertIn("-site:hostname", prompt.task_focus)
        self.assertIn("rewrite the query once", prompt.task_focus)
        self.assertIn("published/updated date", prompt.task_focus)
        self.assertIn("xiaohongshu.com", prompt.role)
        self.assertIn("spam.example", prompt.role)

    def test_invalid_search_task_pack_is_rejected_before_runner(self) -> None:
        calls: list[dict] = []

        class Runner:
            def run(self, **kwargs):
                calls.append(kwargs)
                raise AssertionError("runner must not execute for invalid search input")

        tool = _make_delegate_tool(
            "sid",
            SubagentDef(
                name="search_test",
                tool_name="search_test",
                summary="test",
                system_prompt="test",
                kind="search",
            ),
            Runner(),
            SubagentRuntimeConfig(None, 1, 1, 10, 1000),
            lambda _tool: True,
        )

        result = ToolExecutor(tools=[tool]).execute(
            "search_test",
            {"objective": "latest Unity version"},
        )
        payload = json.loads(result.summary)

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error_code"], "invalid_search_task_pack")
        self.assertTrue(payload["limits"]["validation_failed"])
        self.assertEqual(calls, [])

    def test_botspec_rejects_unknown_subagent_and_invalid_budget(self) -> None:
        spec = load_botspec(Path("bots/lingye-copilot-qq/bot.yaml"))
        bad = replace(
            spec,
            agents=SubagentSpec(
                include=("unknown",),
                agents={"unknown": SubagentBudgetSpec(max_model_turns=0)},
            ),
        )

        errors = [issue for issue in validate_botspec(bad) if issue.level == "error"]
        messages = "\n".join(issue.message for issue in errors)

        self.assertIn("未知 subagent", messages)
        self.assertIn("max_model_turns", messages)

    def test_botspec_accepts_custom_subagent(self) -> None:
        spec = load_botspec(Path("bots/lingye-copilot-qq/bot.yaml"))
        custom = CustomSubagentSpec(
            name="jira_reader",
            tool_name="query_jira",
            summary="只读查询 Jira",
            selector=ToolSelectorSpec(
                any=(ToolMatchRule(categories=("mcp",), mcp_risk=("readonly",)),)
            ),
            budget=SubagentBudgetSpec(),
            prompt_path="prompts/persona.md",  # 复用现有文件，仅校验指针存在
        )
        bot = replace(spec, agents=replace(spec.agents, custom=(custom,)))

        errors = [issue for issue in validate_botspec(bot) if issue.level == "error"]
        self.assertEqual(errors, [])

    def test_adapter_forge_is_owner_codex_bound_and_validates_source_envelope(self) -> None:
        calls: list[dict] = []

        class Runner:
            def run(self, **kwargs):
                calls.append(kwargs)
                raise AssertionError("runner must not execute for invalid approval")

        definition = BUILTIN_SUBAGENTS["adapter_forge"]
        tool = _make_delegate_tool(
            "sid",
            definition,
            Runner(),
            SubagentRuntimeConfig(None, 1, 1, 10, 1000),
            lambda _tool: True,
        )

        result = ToolExecutor(tools=[tool]).execute(
            "forge_open_source_adapter",
            {
                "objective": "Integrate the approved adapter",
                "write_scope": ["src/chatcopilot/external_tools/sample"],
                "source_url": "https://github.com/example/sample",
                "approved_ref": "main",
                "candidate_digest": "not-a-digest",
                "license_evidence": "MIT LICENSE",
                "integration_intent": "Add a readonly sample adapter",
                "resource_name": "sample",
            },
        )
        payload = json.loads(result.summary)

        self.assertEqual(tool.requires_role, "owner")
        self.assertEqual(tool.metadata["execution_boundary"], "codex")
        self.assertEqual(payload["error_code"], "invalid_adapter_approval")
        self.assertIn("approved_ref", payload["summary"])
        self.assertIn("candidate_digest", payload["summary"])
        self.assertEqual(calls, [])

    def test_adapter_forge_accepts_matching_immutable_source_envelope(self) -> None:
        calls: list[dict] = []

        class Runner:
            def run(self, **kwargs):
                calls.append(kwargs)
                return SimpleNamespace(ok=True, summary="adapter ready", outputs=())

        definition = BUILTIN_SUBAGENTS["adapter_forge"]
        tool = _make_delegate_tool(
            "sid",
            definition,
            Runner(),
            SubagentRuntimeConfig(None, 1, 1, 10, 1000),
            lambda _tool: True,
        )
        args = {
            "objective": "Integrate the approved adapter",
            "write_scope": ["src/chatcopilot/external_tools/sample"],
            "source_url": "https://github.com/example/sample",
            "approved_ref": "a" * 40,
            "license_evidence": "MIT LICENSE",
            "integration_intent": "Add a readonly sample adapter",
            "resource_name": "sample",
        }
        args["candidate_digest"] = _adapter_candidate_digest(args)
        envelope = AdapterApprovalEnvelope(
            resource_name=args["resource_name"],
            source_url=args["source_url"],
            approved_ref=args["approved_ref"],
            license_evidence=args["license_evidence"],
            integration_intent=args["integration_intent"],
        )
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            bot_path = root / "bots" / "sample-bot" / "bot.yaml"
            bot_path.parent.mkdir(parents=True)
            bot_path.write_text("id: sample-bot\n", encoding="utf-8")
            args["bot"] = str(bot_path)
            AdapterApprovalStore.for_bot(bot_path).approve(
                envelope=envelope,
                candidate_digest=args["candidate_digest"],
                approved_by="owner-1",
            )

            class WorkspaceService:
                def resolve_workspace(self, *, create: bool = True):
                    return SimpleNamespace(root=root, user_id="owner-1")

                def resolve_workspace_root(self, workspace):
                    return workspace.root

            result = ToolExecutor(
                tools=[tool],
                workspace_service=WorkspaceService(),
            ).execute(
                "forge_open_source_adapter",
                args,
            )
            replay = ToolExecutor(
                tools=[tool],
                workspace_service=WorkspaceService(),
            ).execute(
                "forge_open_source_adapter",
                args,
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.summary, "adapter ready")
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            json.loads(replay.summary)["error_code"],
            "adapter_approval_required",
        )

    def test_adapter_forge_rejects_a_self_signed_unapproved_envelope(self) -> None:
        calls: list[dict] = []

        class Runner:
            def run(self, **kwargs):
                calls.append(kwargs)
                return SimpleNamespace(ok=True, summary="unexpected", outputs=())

        definition = BUILTIN_SUBAGENTS["adapter_forge"]
        tool = _make_delegate_tool(
            "sid",
            definition,
            Runner(),
            SubagentRuntimeConfig(None, 1, 1, 10, 1000),
            lambda _tool: True,
        )
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            bot_path = root / "bots" / "sample-bot" / "bot.yaml"
            bot_path.parent.mkdir(parents=True)
            bot_path.write_text("id: sample-bot\n", encoding="utf-8")
            args = {
                "objective": "Integrate an unapproved adapter",
                "write_scope": ["src/chatcopilot/external_tools/sample"],
                "source_url": "https://github.com/example/sample",
                "approved_ref": "a" * 40,
                "license_evidence": "MIT LICENSE",
                "integration_intent": "Add a readonly sample adapter",
                "resource_name": "sample",
                "bot": str(bot_path),
            }
            args["candidate_digest"] = _adapter_candidate_digest(args)

            class WorkspaceService:
                def resolve_workspace(self, *, create: bool = True):
                    return SimpleNamespace(root=root, user_id="owner-1")

                def resolve_workspace_root(self, workspace):
                    return workspace.root

            result = ToolExecutor(
                tools=[tool],
                workspace_service=WorkspaceService(),
            ).execute("forge_open_source_adapter", args)

        payload = json.loads(result.summary)
        self.assertEqual(payload["error_code"], "adapter_approval_required")
        self.assertIn("record not found", payload["summary"])
        self.assertEqual(calls, [])

    def test_codex_backend_exposes_only_configured_adapter_delegate(self) -> None:
        captured_tool_names: set[str] = set()

        class Backend:
            capabilities = BackendCapabilities(
                names=frozenset({CAPABILITY_CHAT, CAPABILITY_TOOLS}),
                tool_names=frozenset({"forge_open_source_adapter", "start_code_task"}),
            )

            def open_session(self, request):
                captured_tool_names.update(request.allowed_tool_names)
                return BackendSessionRef("codex", "session")

        runtime = AgentRuntime(
            llm=_FakeLLM(ChatResult(content="done")),
            tools=(_tool("start_code_task", category="development.task.write"),),
            tools_schema=(),
            runtime_config=ChatConfig(),
            subagents=SubagentSpec(
                include=("adapter_forge", "mcp_query"),
                agents={
                    "adapter_forge": SubagentBudgetSpec(
                        max_model_turns=1,
                        max_tool_calls=1,
                    ),
                    "mcp_query": SubagentBudgetSpec(max_model_turns=1, max_tool_calls=1),
                },
            ),
            agent_backend="codex",
        )

        with mock.patch("chatcopilot.agent.runtime.build_backend", return_value=Backend()):
            runtime.new_session(
                session_id="sid",
                system_baseline="baseline",
                permission_filter=lambda _tool: None,
            )

        self.assertIn("forge_open_source_adapter", captured_tool_names)
        self.assertNotIn("query_approved_sources", captured_tool_names)

    def test_codex_eval_policy_exposes_all_configured_delegate_tools(self) -> None:
        captured_tool_names: set[str] = set()

        class Backend:
            capabilities = BackendCapabilities(
                names=frozenset({CAPABILITY_CHAT, CAPABILITY_TOOLS}),
                tool_names=frozenset(
                    {
                        "forge_open_source_adapter",
                        "query_approved_sources",
                        "start_code_task",
                    }
                ),
            )

            def open_session(self, request):
                captured_tool_names.update(request.allowed_tool_names)
                return BackendSessionRef("codex", "session")

        runtime = AgentRuntime(
            llm=_FakeLLM(ChatResult(content="done")),
            tools=(_tool("start_code_task", category="development.task.write"),),
            tools_schema=(),
            runtime_config=ChatConfig(),
            subagents=SubagentSpec(
                include=("adapter_forge", "mcp_query"),
                agents={
                    "adapter_forge": SubagentBudgetSpec(
                        max_model_turns=1,
                        max_tool_calls=1,
                    ),
                    "mcp_query": SubagentBudgetSpec(
                        max_model_turns=1,
                        max_tool_calls=1,
                    ),
                },
                codex=CodexMainSessionPolicy(allow_delegate_tools=True),
            ),
            agent_backend="codex",
        )

        with mock.patch("chatcopilot.agent.runtime.build_backend", return_value=Backend()):
            runtime.new_session(
                session_id="sid-eval-delegates",
                system_baseline="baseline",
                permission_filter=lambda _tool: None,
            )

        self.assertIn("forge_open_source_adapter", captured_tool_names)
        self.assertIn("query_approved_sources", captured_tool_names)

    def test_botspec_allows_write_risk_but_rejects_empty_selector_custom(self) -> None:
        spec = load_botspec(Path("bots/lingye-copilot-qq/bot.yaml"))
        write_custom = CustomSubagentSpec(
            name="jira_writer",
            tool_name="write_jira",
            summary="写 Jira",
            selector=ToolSelectorSpec(any=(ToolMatchRule(categories=("mcp",), mcp_risk=("write",)),)),
            prompt_path="prompts/persona.md",
        )
        empty_custom = CustomSubagentSpec(
            name="empty_one",
            tool_name="noop",
            summary="空 selector",
            selector=ToolSelectorSpec(),
            prompt_path="prompts/persona.md",
        )
        write_bot = replace(spec, agents=replace(spec.agents, custom=(write_custom,)))
        self.assertEqual(
            [issue for issue in validate_botspec(write_bot) if issue.level == "error"],
            [],
        )

        bot = replace(spec, agents=replace(spec.agents, custom=(empty_custom,)))

        messages = "\n".join(
            issue.message for issue in validate_botspec(bot) if issue.level == "error"
        )
        self.assertIn("selector.any 不能为空", messages)

    def test_developer_subagent_only_sees_allowed_tools(self) -> None:
        fake_llm = _FakeLLM(ChatResult(content="代码检查完成"))
        tools = build_subagent_tools(
            session_id="sid",
            subagents=SubagentSpec(
                include=("developer",),
                agents={"developer": SubagentBudgetSpec(max_model_turns=1, max_tool_calls=1)},
            ),
            main_llm=fake_llm,
            main_config=ChatConfig(),
            base_tools=(
                _tool("read_file", category="dev.files"),
                _tool("read_text_head", category="agent.workspace"),
                _tool("win_read_file", category="filesystem.windows.read"),
            ),
        )

        result = ToolExecutor(tools=list(tools)).execute(
            "delegate_development",
            {"task": "检查代码", "write_scope": ["tests"]},
        )
        payload = json.loads(result.summary)

        self.assertTrue(payload["ok"])
        self.assertIn("read_file", fake_llm.seen_tools)
        self.assertNotIn("read_text_head", fake_llm.seen_tools)
        self.assertNotIn("win_read_file", fake_llm.seen_tools)

    def test_subagent_emits_structured_result_via_submit_result(self) -> None:
        scripted = _ScriptedLLM(
            [
                _submit_call(
                    {
                        "summary": "内存峰值上升 12%",
                        "evidence": [{"claim": "peak +12%", "source": "results/diff.csv"}],
                        "outputs": ["results/diff.csv"],
                        "next_steps": ["复测下一版本"],
                        "ok": True,
                    }
                ),
                ChatResult(content="完成"),
            ]
        )
        tools = build_subagent_tools(
            session_id="sid",
            subagents=SubagentSpec(
                include=("developer",),
                agents={"developer": SubagentBudgetSpec(max_model_turns=2, max_tool_calls=2)},
            ),
            main_llm=scripted,
            main_config=ChatConfig(),
            base_tools=(_tool("read_file", category="dev.files"),),
        )

        result = ToolExecutor(tools=list(tools)).execute(
            "delegate_development", {"task": "检查代码", "write_scope": ["tests"]}
        )
        payload = json.loads(result.summary)

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["summary"], "内存峰值上升 12%")
        self.assertEqual(payload["evidence"], [{"claim": "peak +12%", "source": "results/diff.csv"}])
        self.assertIn("results/diff.csv", payload["outputs"])
        self.assertEqual(payload["next_steps"], ["复测下一版本"])
        self.assertNotIn("partial", payload.get("limits", {}))

    def test_subagent_falls_back_to_partial_without_submit_result(self) -> None:
        fake_llm = _FakeLLM(ChatResult(content="我直接用自然语言回答了"))
        tools = build_subagent_tools(
            session_id="sid",
            subagents=SubagentSpec(
                include=("developer",),
                agents={"developer": SubagentBudgetSpec(max_model_turns=1, max_tool_calls=1)},
            ),
            main_llm=fake_llm,
            main_config=ChatConfig(),
            base_tools=(_tool("read_file", category="dev.files"),),
        )

        result = ToolExecutor(tools=list(tools)).execute(
            "delegate_development", {"task": "检查代码", "write_scope": ["tests"]}
        )
        payload = json.loads(result.summary)

        self.assertTrue(payload["limits"]["partial"])
        self.assertIn("自然语言", payload["summary"])

    def test_browser_reader_always_closes_browser_after_task(self) -> None:
        close_calls: list[dict] = []
        browser_close = ToolDef(
            name="browser_close",
            summary="close browser",
            properties={},
            required=[],
            handler=lambda args: (close_calls.append(args) or "closed", [], None),
        )
        scripted = _ScriptedLLM(
            [
                _submit_call({"ok": True, "summary": "rendered page read"}),
                ChatResult(content="done"),
            ]
        )
        tools = build_subagent_tools(
            session_id="sid",
            subagents=SubagentSpec(
                include=("browser_reader",),
                agents={
                    "browser_reader": SubagentBudgetSpec(
                        max_model_turns=2,
                        max_tool_calls=2,
                    )
                },
            ),
            main_llm=scripted,
            main_config=ChatConfig(),
            base_tools=(browser_close,),
        )

        result = ToolExecutor(tools=list(tools)).execute(
            "browse_dynamic_page",
            {
                "objective": "read https://example.com",
                "resources": ["https://example.com"],
            },
        )

        self.assertTrue(json.loads(result.summary)["ok"])
        self.assertEqual(close_calls, [{}])

    def test_subagent_never_sees_user_facing_tool(self) -> None:
        fake_llm = _FakeLLM(ChatResult(content="done"))
        sender = _tool("send_files_to_user", category="dev.files")
        sender.metadata.update({"user_facing": True})
        tools = build_subagent_tools(
            session_id="sid",
            subagents=SubagentSpec(
                include=("developer",),
                agents={"developer": SubagentBudgetSpec(max_model_turns=1, max_tool_calls=1)},
            ),
            main_llm=fake_llm,
            main_config=ChatConfig(),
            base_tools=(_tool("read_file", category="dev.files"), sender),
        )

        ToolExecutor(tools=list(tools)).execute(
            "delegate_development", {"task": "整理产物", "write_scope": ["tests"]}
        )

        self.assertIn("read_file", fake_llm.seen_tools)
        self.assertNotIn("send_files_to_user", fake_llm.seen_tools)

    def test_subagent_scoped_mcp_tool_is_hidden_from_main_schema(self) -> None:
        fake_llm = _FakeLLM(ChatResult(content="GitHub 查询完成"))
        normal = _tool("normal_tool", category="agent")
        github = _tool("github_search_repositories", category="mcp", owner="github")
        github.metadata.update(
            {
                "mcp_exposure": "subagent",
                "mcp_allowed_subagents": ["mcp_query"],
                "mcp_risk": "readonly",
            }
        )
        runtime = AgentRuntime(
            llm=fake_llm,
            tools=(normal,),
            tools_schema=(),
            runtime_config=SimpleNamespace(
                runtime=SimpleNamespace(
                    max_context_tokens=16000,
                    sliding_window_turns=3,
                    tool_result_summary_max_tokens=500,
                    max_tool_retries=1,
                )
            ),
            subagents=SubagentSpec(
                include=("mcp_query",),
                agents={"mcp_query": SubagentBudgetSpec(max_model_turns=1, max_tool_calls=1)},
            ),
            subagent_tools=(normal, github),
        )

        session = runtime.new_session(session_id="sid", system_baseline="baseline")
        schema_names = {entry["function"]["name"] for entry in session.tools_schema}

        self.assertIn("normal_tool", schema_names)
        self.assertIn("query_approved_sources", schema_names)
        self.assertNotIn("github_search_repositories", schema_names)

        result = session.executor.execute("query_approved_sources", {"task": "查 repo"})
        payload = json.loads(result.summary)

        self.assertTrue(payload["ok"])
        self.assertIn("github_search_repositories", fake_llm.seen_tools)


    def test_iteration_cap_does_not_override_submitted_ok_true(self) -> None:
        """When subagent uses all iterations but successfully calls submit_result(ok=True),
        the runner must return ok=True, not override with iteration_cap failure."""
        scripted = _ScriptedLLM(
            [
                # iter 0: call a work tool
                ChatResult(
                    content="",
                    tool_calls=[
                        {
                            "id": "call_search",
                            "type": "function",
                            "function": {"name": "read_file", "arguments": "{}"},
                        }
                    ],
                    finish_reason="tool_calls",
                ),
                # iter 1 (last allowed): call submit_result with ok=True
                _submit_call(
                    {
                        "summary": "Found relevant data",
                        "findings": [{"text": "key finding"}],
                        "confidence": "medium",
                        "ok": True,
                    }
                ),
            ]
        )
        tools = build_subagent_tools(
            session_id="sid",
            subagents=SubagentSpec(
                include=("developer",),
                agents={"developer": SubagentBudgetSpec(max_model_turns=2, max_tool_calls=3)},
            ),
            main_llm=scripted,
            main_config=ChatConfig(),
            base_tools=(_tool("read_file", category="dev.files"),),
        )

        result = ToolExecutor(tools=list(tools)).execute(
            "delegate_development", {"task": "检查代码", "write_scope": ["tests"]}
        )
        payload = json.loads(result.summary)

        self.assertTrue(payload["ok"], f"Expected ok=True but got payload: {payload}")
        self.assertEqual(payload["summary"], "Found relevant data")
        self.assertNotEqual(payload.get("error_code"), "iteration_cap")

    def test_iteration_cap_still_fails_when_submit_result_says_ok_false(self) -> None:
        """When subagent hits iteration_cap AND submitted ok=False, result must be ok=False."""
        scripted = _ScriptedLLM(
            [
                ChatResult(
                    content="",
                    tool_calls=[
                        {
                            "id": "call_search",
                            "type": "function",
                            "function": {"name": "read_file", "arguments": "{}"},
                        }
                    ],
                    finish_reason="tool_calls",
                ),
                _submit_call(
                    {
                        "summary": "MCP quota exhausted",
                        "ok": False,
                        "error_code": "mcp_quota_exceeded",
                    }
                ),
            ]
        )
        tools = build_subagent_tools(
            session_id="sid",
            subagents=SubagentSpec(
                include=("developer",),
                agents={"developer": SubagentBudgetSpec(max_model_turns=2, max_tool_calls=3)},
            ),
            main_llm=scripted,
            main_config=ChatConfig(),
            base_tools=(_tool("read_file", category="dev.files"),),
        )

        result = ToolExecutor(tools=list(tools)).execute(
            "delegate_development", {"task": "检查代码", "write_scope": ["tests"]}
        )
        payload = json.loads(result.summary)

        self.assertFalse(payload["ok"])


if __name__ == "__main__":
    unittest.main()
