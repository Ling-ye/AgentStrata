"""Regression tests for Feishu role permissions and debug mode toggles."""
from __future__ import annotations

import json
import os
import shutil
import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from chatcopilot.core.llm_client import ChatResult
from chatcopilot.contracts.agent import AgentTask
from chatcopilot.agent.session import AgentSession
from chatcopilot.agent.context.prompt_plan import PromptBuildInput, PromptPlanBuilder, render_native_prefix
from chatcopilot.contracts.prompt import BotPromptProfile
from chatcopilot.contracts.workspace import WORKSPACE_SCOPE_GROUP_SHARED
from chatcopilot.agent.tools.executor import ToolExecutor
from chatcopilot.middleware.acp.session_state import SessionState
from chatcopilot.core.workspace_runtime import (
    Workspace,
    normalize_chat_kind as normalize_workspace_chat_kind,
    resolve_workspace,
)
from chatcopilot.middleware.access_control import AssistantMode, Role, default_debug_mode
from chatcopilot.middleware.access_control import (
    can_select_general_mode,
    can_toggle_debug,
    default_assistant_mode,
    normalize_chat_kind,
)
from chatcopilot.middleware.acp.meta_commands import (
    _build_set_debug_mode_tool,
    _build_set_assistant_mode_tool,
    _handle_assistant_mode_command,
    _handle_debug_command,
    _parse_debug_command,
)
from chatcopilot.middleware.acp.agent_bridge import _refresh_session_prompt_plan
from chatcopilot.contracts.tools import build_openai_schema


def _test_prompt_plan(workspace: Workspace, **kwargs):
    role = kwargs.get("role", Role.USER)
    mode = kwargs.get("assistant_mode", AssistantMode.PERFORMANCE)
    mode_prompts = kwargs.get("mode_prompts", {})
    return PromptPlanBuilder().build(
        PromptBuildInput(
            profile=BotPromptProfile(
                identity=kwargs.get("bot_system_prompt") or "Test assistant",
                response_style="Return concise test responses.",
                mode_styles={
                    key.value if hasattr(key, "value") else str(key): value
                    for key, value in mode_prompts.items()
                },
            ),
            backend="native",
            model=None,
            role=role.value if hasattr(role, "value") else str(role),
            channel_kind=(
                "group"
                if workspace.scope == WORKSPACE_SCOPE_GROUP_SHARED
                else "private"
            ),
            session_policy=f"当前可信角色：{role.value}。",
            mode=mode.value if hasattr(mode, "value") else str(mode),
        )
    )


def render_test_prompt(workspace: Workspace, **kwargs) -> str:
    return "\n".join(
        message["content"]
        for message in render_native_prefix(_test_prompt_plan(workspace, **kwargs))
    )


def _rendered_session_prompt(session: SessionState) -> str:
    return "\n".join(
        message["content"]
        for message in render_native_prefix(session.require_session().prompt_plan)
    )


class _FakeLLM:
    def __init__(self, results: list[ChatResult]) -> None:
        self.results = list(results)

    def chat(self, **kwargs):
        if not self.results:
            return ChatResult(content="")
        return self.results.pop(0)


def _tool_call(name: str, args: dict) -> dict:
    return {
        "id": "call_mode",
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(args, ensure_ascii=False),
        },
    }


# 业务模式提示词现在由 BotSpec 提供（不再硬编码在平台层）；测试用最小化的 Sample
# 模式片段，覆盖与断言对应的关键口径。
_MODE_PROMPTS = {
    "performance": (
        "# SampleGame 性能分析模式\n\n"
        "主要处理与 SampleGame 性能分析工具直接相关的请求：\n"
        "- 外部网站搜索：当用户明确要求时调用搜索工具。\n"
    ),
    "general": (
        "# SampleGame 通用模式\n\n"
        "通用模式可以处理飞书文档总结、周报整理、文本改写、方案讨论等任务。\n"
        "- 外部网站搜索：当用户明确要求时调用搜索工具。\n"
    ),
}


class DebugModeAccessTests(unittest.TestCase):
    def test_debug_mode_defaults_to_final_answer_only_for_all_roles(self) -> None:
        for role in Role:
            with self.subTest(role=role):
                self.assertFalse(default_debug_mode(role))

    def test_only_p2p_owner_can_toggle_debug(self) -> None:
        self.assertTrue(can_toggle_debug(Role.OWNER, "p2p"))
        self.assertTrue(can_toggle_debug(Role.OWNER, "p2p_msg", "oc_private"))
        self.assertTrue(can_toggle_debug(Role.OWNER, "", "oc_private"))
        self.assertFalse(can_toggle_debug(Role.OWNER, "group"))
        self.assertFalse(can_toggle_debug(Role.OWNER, "group_at_msg", "oc_group"))
        self.assertFalse(can_toggle_debug(Role.ADMIN, "p2p"))
        self.assertFalse(can_toggle_debug(Role.USER, "p2p"))

    def test_assistant_mode_defaults_to_performance_for_all_roles(self) -> None:
        for role in Role:
            with self.subTest(role=role):
                self.assertEqual(default_assistant_mode(role), AssistantMode.PERFORMANCE)

    def test_only_p2p_owner_can_select_general_mode(self) -> None:
        self.assertTrue(can_select_general_mode(Role.OWNER, "p2p"))
        self.assertTrue(can_select_general_mode(Role.OWNER, "p2p_msg", "oc_private"))
        self.assertTrue(can_select_general_mode(Role.OWNER, "", "oc_private"))
        self.assertFalse(can_select_general_mode(Role.OWNER, "group"))
        self.assertFalse(can_select_general_mode(Role.OWNER, "group_at_msg", "oc_group"))
        self.assertFalse(can_select_general_mode(Role.ADMIN, "p2p"))
        self.assertFalse(can_select_general_mode(Role.USER, "p2p"))

    def test_explicit_chat_kind_wins_over_oc_chat_id(self) -> None:
        self.assertEqual(normalize_chat_kind("p2p", "oc_private"), "p2p")
        self.assertEqual(normalize_chat_kind("p2p_msg", "oc_private"), "p2p")
        self.assertEqual(normalize_chat_kind("group_at_msg", "oc_group"), "group")
        self.assertEqual(normalize_chat_kind("", "oc_private"), "p2p")
        self.assertEqual(normalize_workspace_chat_kind("p2p_msg", "oc_private"), "p2p")
        self.assertEqual(normalize_workspace_chat_kind("group_msg", "oc_group"), "group")
        self.assertEqual(normalize_workspace_chat_kind("", "oc_private"), "p2p")

    def test_workspace_uses_explicit_feishu_message_type(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(
                os.environ,
                {
                    "CHATCOPILOT_WORKSPACE": "",
                    "CHATCOPILOT_WORKSPACE_ROOT": tmp,
                    "CHATCOPILOT_CHAT_KIND": "p2p_msg",
                    "CHATCOPILOT_CHAT_ID": "oc_private",
                    "CHATCOPILOT_USER_ID": "ou_test",
                },
            ):
                ws = resolve_workspace(create=False)
                self.assertEqual(ws.root, Path(tmp).resolve() / "p2p_ou_test")
                self.assertEqual(ws.chat_kind, "p2p")

            with patch.dict(
                os.environ,
                {
                    "CHATCOPILOT_WORKSPACE": "",
                    "CHATCOPILOT_WORKSPACE_ROOT": tmp,
                    "CHATCOPILOT_CHAT_KIND": "",
                    "CHATCOPILOT_CHAT_ID": "oc_private",
                    "CHATCOPILOT_USER_ID": "ou_test",
                },
            ):
                ws = resolve_workspace(create=False)
                self.assertEqual(ws.root, Path(tmp).resolve() / "p2p_ou_test")
                self.assertEqual(ws.chat_kind, "p2p")

            with patch.dict(
                os.environ,
                {
                    "CHATCOPILOT_WORKSPACE": "",
                    "CHATCOPILOT_WORKSPACE_ROOT": tmp,
                    "CHATCOPILOT_CHAT_KIND": "group_at_msg",
                    "CHATCOPILOT_CHAT_ID": "oc_group",
                    "CHATCOPILOT_USER_ID": "ou_test",
                },
            ):
                ws = resolve_workspace(create=False)
                self.assertEqual(
                    ws.root,
                    Path(tmp).resolve() / "group_oc_group" / "user_ou_test",
                )
                self.assertEqual(ws.chat_kind, "group")

    def test_slash_debug_commands_are_preserved(self) -> None:
        cases = {
            "/debug on": "on",
            "/debug off": "off",
            "/debug status": "status",
            "/debug": "status",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(_parse_debug_command(text), expected)

    def _session_stub(
        self,
        *,
        role: Role,
        debug_mode: bool,
        chat_kind: str = "p2p",
        chat_id: str | None = None,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            role=role,
            debug_mode=debug_mode,
            workspace=SimpleNamespace(chat_kind=chat_kind, chat_id=chat_id),
        )

    def test_owner_can_enable_debug_with_slash_command(self) -> None:
        session = self._session_stub(role=Role.OWNER, debug_mode=False)

        reply = _handle_debug_command(session, "/debug on")

        self.assertTrue(session.debug_mode)
        self.assertIn("调试模式已开启", reply or "")

    def test_owner_can_disable_debug_with_slash_command(self) -> None:
        session = self._session_stub(role=Role.OWNER, debug_mode=True)

        reply = _handle_debug_command(session, "/debug off")

        self.assertFalse(session.debug_mode)
        self.assertIn("调试模式已关闭", reply or "")

    def test_user_cannot_enable_debug_with_slash_command(self) -> None:
        session = self._session_stub(role=Role.USER, debug_mode=False)

        reply = _handle_debug_command(session, "/debug on")

        self.assertFalse(session.debug_mode)
        self.assertIn("仅限 Owner 私聊", reply or "")

    def test_group_owner_cannot_enable_debug_with_slash_command(self) -> None:
        session = self._session_stub(
            role=Role.OWNER,
            debug_mode=False,
            chat_kind="group_at_msg",
            chat_id="oc_group",
        )

        reply = _handle_debug_command(session, "/debug on")

        self.assertFalse(session.debug_mode)
        self.assertIn("群聊固定", reply or "")

    def test_group_owner_cannot_enable_debug_with_natural_language(self) -> None:
        session = self._session_stub(
            role=Role.OWNER,
            debug_mode=False,
            chat_kind="group_at_msg",
            chat_id="oc_group",
        )

        reply = _handle_debug_command(session, "@SampleGame性能助手 开启debug模式")

        self.assertFalse(session.debug_mode)
        self.assertIn("群聊固定", reply or "")

    def _build_mode_session(
        self,
        *,
        role: Role,
        assistant_mode: AssistantMode,
        llm_results: list[ChatResult],
        chat_kind: str = "p2p",
        chat_id: str | None = None,
    ) -> SessionState:
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        ws = Workspace(
            root=Path(tmp),
            chat_kind=chat_kind,
            chat_id=(
                chat_id
                if chat_id is not None
                else ("oc_group" if chat_kind == "group" else None)
            ),
            user_id=f"ou_{role.value}",
            user_name=role.value,
            scope=(
                WORKSPACE_SCOPE_GROUP_SHARED
                if normalize_chat_kind(chat_kind, chat_id) == "group"
                else "actor"
            ),
        ).ensure()
        state_ref: dict = {}
        mode_tool = _build_set_assistant_mode_tool(
            lambda: state_ref["session"],
            refresh_prompt_plan=_refresh_session_prompt_plan,
        )
        debug_tool = _build_set_debug_mode_tool(lambda: state_ref["session"])
        tools = [mode_tool, debug_tool]
        agent_session = AgentSession(
            session_id="sid",
            llm=_FakeLLM(llm_results),
            executor=ToolExecutor(tools=tools),
            tools_schema=[build_openai_schema(tool) for tool in tools],
            prompt_plan=_test_prompt_plan(
                ws,
                role=role,
                assistant_mode=assistant_mode,
                mode_prompts=_MODE_PROMPTS,
            ),
        )
        agent_session.capabilities = SimpleNamespace(  # type: ignore[attr-defined]
            tool_names=frozenset(tool.name for tool in tools)
        )
        state = SessionState(
            session_id="sid",
            workspace=ws,
            role=role,
            assistant_mode=assistant_mode,
            runtime=SimpleNamespace(
                agent_backend="native",
                prompt_profile=BotPromptProfile(
                    identity="Test assistant",
                    response_style="Return concise test responses.",
                    mode_styles={
                        key.value if hasattr(key, "value") else str(key): value
                        for key, value in _MODE_PROMPTS.items()
                    },
                ),
                capability_policies=(),
                skills=(),
            ),  # type: ignore[arg-type]
            session=agent_session,
        )
        state_ref["session"] = state
        return state

    def test_owner_can_switch_to_general_mode_via_llm_tool_call(self) -> None:
        session = self._build_mode_session(
            role=Role.OWNER,
            assistant_mode=AssistantMode.PERFORMANCE,
            llm_results=[
                ChatResult(
                    tool_calls=[
                        _tool_call(
                            "set_assistant_mode",
                            {"mode": "general", "reason": "用户要求切到通用模式"},
                        )
                    ]
                ),
                ChatResult(content="已切换到通用模式。"),
            ],
        )

        reply = session.session.run_task(AgentTask(text="切到通用模式"), on_event=lambda e: None).final_text

        self.assertEqual(session.assistant_mode, AssistantMode.GENERAL)
        self.assertIn("已切换到通用模式", reply or "")
        self.assertIn("SampleGame 通用模式", _rendered_session_prompt(session))

    def test_user_cannot_switch_to_general_mode_via_llm_tool_call(self) -> None:
        session = self._build_mode_session(
            role=Role.USER,
            assistant_mode=AssistantMode.PERFORMANCE,
            llm_results=[
                ChatResult(
                    tool_calls=[
                        _tool_call(
                            "set_assistant_mode",
                            {"mode": "general", "reason": "用户要求切到通用模式"},
                        )
                    ]
                ),
                ChatResult(content="通用模式仅限 Owner 私聊可用。"),
            ],
        )

        reply = session.session.run_task(AgentTask(text="切到通用模式"), on_event=lambda e: None).final_text

        self.assertEqual(session.assistant_mode, AssistantMode.PERFORMANCE)
        self.assertIn("SampleGame 性能分析模式", _rendered_session_prompt(session))
        self.assertIn("通用模式仅限 Owner 私聊", reply or "")

    def test_group_owner_cannot_switch_to_general_mode_via_llm_tool_call(self) -> None:
        session = self._build_mode_session(
            role=Role.OWNER,
            assistant_mode=AssistantMode.GENERAL,
            chat_kind="group_at_msg",
            chat_id="oc_group",
            llm_results=[
                ChatResult(
                    tool_calls=[
                        _tool_call(
                            "set_assistant_mode",
                            {"mode": "general", "reason": "群聊里要求切到通用模式"},
                        )
                    ]
                ),
                ChatResult(content="群聊固定为性能分析模式。"),
            ],
        )

        reply = session.session.run_task(AgentTask(text="切到通用模式"), on_event=lambda e: None).final_text

        self.assertEqual(session.assistant_mode, AssistantMode.PERFORMANCE)
        self.assertIn("SampleGame 性能分析模式", _rendered_session_prompt(session))
        self.assertIn("群聊固定", reply or "")

    def test_group_owner_cannot_switch_to_general_mode_with_natural_language(self) -> None:
        session = self._build_mode_session(
            role=Role.OWNER,
            assistant_mode=AssistantMode.PERFORMANCE,
            chat_kind="group_at_msg",
            chat_id="oc_group",
            llm_results=[],
        )

        reply = _handle_assistant_mode_command(
            session,
            "@SampleGame性能助手 切换为通用模式",
            refresh_prompt_plan=_refresh_session_prompt_plan,
        )

        self.assertEqual(session.assistant_mode, AssistantMode.PERFORMANCE)
        self.assertIn("SampleGame 性能分析模式", _rendered_session_prompt(session))
        self.assertIn("群聊固定", reply or "")

    def test_owner_can_switch_back_to_performance_mode_via_llm_tool_call(self) -> None:
        session = self._build_mode_session(
            role=Role.OWNER,
            assistant_mode=AssistantMode.GENERAL,
            llm_results=[
                ChatResult(
                    tool_calls=[
                        _tool_call(
                            "set_assistant_mode",
                            {"mode": "performance", "reason": "用户要求回到性能分析模式"},
                        )
                    ]
                ),
                ChatResult(content="已切换到性能分析模式。"),
            ],
        )

        reply = session.session.run_task(AgentTask(text="回到性能分析模式"), on_event=lambda e: None).final_text

        self.assertEqual(session.assistant_mode, AssistantMode.PERFORMANCE)
        self.assertIn("已切换到性能分析模式", reply or "")
        self.assertIn("SampleGame 性能分析模式", _rendered_session_prompt(session))

    def test_owner_can_enable_debug_mode_via_llm_tool_call(self) -> None:
        session = self._build_mode_session(
            role=Role.OWNER,
            assistant_mode=AssistantMode.PERFORMANCE,
            llm_results=[
                ChatResult(
                    tool_calls=[
                        _tool_call(
                            "set_debug_mode",
                            {"mode": "on", "reason": "用户要求开启调试模式"},
                        )
                    ]
                ),
                ChatResult(content="调试模式已开启。"),
            ],
        )

        reply = session.session.run_task(AgentTask(text="开启调试模式"), on_event=lambda e: None).final_text

        self.assertTrue(session.debug_mode)
        self.assertIn("调试模式已开启", reply or "")

    def test_user_cannot_enable_debug_mode_via_llm_tool_call(self) -> None:
        session = self._build_mode_session(
            role=Role.USER,
            assistant_mode=AssistantMode.PERFORMANCE,
            llm_results=[
                ChatResult(
                    tool_calls=[
                        _tool_call(
                            "set_debug_mode",
                            {"mode": "on", "reason": "用户要求开启调试模式"},
                        )
                    ]
                ),
                ChatResult(content="调试模式仅限 Owner 私聊开启。"),
            ],
        )

        reply = session.session.run_task(AgentTask(text="开启调试模式"), on_event=lambda e: None).final_text

        self.assertFalse(session.debug_mode)
        self.assertIn("仅限 Owner 私聊", reply or "")

    def test_group_owner_cannot_enable_debug_mode_via_llm_tool_call(self) -> None:
        session = self._build_mode_session(
            role=Role.OWNER,
            assistant_mode=AssistantMode.PERFORMANCE,
            chat_kind="group_at_msg",
            chat_id="oc_group",
            llm_results=[
                ChatResult(
                    tool_calls=[
                        _tool_call(
                            "set_debug_mode",
                            {"mode": "on", "reason": "群聊里要求开启调试模式"},
                        )
                    ]
                ),
                ChatResult(content="群聊固定为性能分析模式，调试模式保持关闭。"),
            ],
        )

        reply = session.session.run_task(AgentTask(text="开启调试模式"), on_event=lambda e: None).final_text

        self.assertFalse(session.debug_mode)
        self.assertIn("群聊固定", reply or "")

    def test_persona_changes_by_assistant_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Workspace(
                root=Path(tmp),
                chat_kind="p2p",
                chat_id=None,
                user_id="ou_owner",
                user_name="owner",
            ).ensure()

            performance_prompt = render_test_prompt(
                ws,
                role=Role.OWNER,
                assistant_mode=AssistantMode.PERFORMANCE,
                mode_prompts=_MODE_PROMPTS,
            )
            general_prompt = render_test_prompt(
                ws,
                role=Role.OWNER,
                assistant_mode=AssistantMode.GENERAL,
                mode_prompts=_MODE_PROMPTS,
            )

        self.assertIn("SampleGame 性能分析模式", performance_prompt)
        self.assertIn("主要处理与 SampleGame 性能分析工具直接相关的请求", performance_prompt)
        self.assertIn("外部网站搜索", performance_prompt)
        self.assertIn("SampleGame 通用模式", general_prompt)
        self.assertIn("飞书文档总结、周报整理", general_prompt)
        self.assertIn("外部网站搜索", general_prompt)
        self.assertIn("当前可信角色：owner", general_prompt)


if __name__ == "__main__":
    unittest.main()
