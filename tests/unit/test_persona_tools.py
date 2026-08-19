from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from chatcopilot.agent.persona import persona_layer_specs, persona_path_for_scope
from chatcopilot.agent.runtime import AgentRuntime
from chatcopilot.agent.tools.builtin import persona_tools
from chatcopilot.agent.tools.registry import discover_tools
from chatcopilot.agent.tools.workspace_context import bind_workspace_service
from chatcopilot.contracts import Role, role_ge, role_value
from chatcopilot.core.caller_context import bind_caller_role
from chatcopilot.external_tools.shared.tool_spec import build_openai_schema
from chatcopilot.middleware.runtime.workspace import MiddlewareWorkspaceService, Workspace


def _p2p_service(root: Path) -> MiddlewareWorkspaceService:
    return MiddlewareWorkspaceService(
        workspace_root=root,
        workspace=Workspace(
            root=root / "p2p_user-001",
            chat_kind="p2p",
            chat_id=None,
            user_id="user-001",
        ).ensure(),
        platform_type="qq",
    )


def _group_service(root: Path) -> MiddlewareWorkspaceService:
    return MiddlewareWorkspaceService(
        workspace_root=root,
        workspace=Workspace(
            root=root / "group_g1" / "shared",
            chat_kind="group",
            chat_id="g1",
            user_id="user-001",
            scope="group_shared",
        ).ensure(),
        platform_type="qq",
    )


class PersonaLegacyLocatorTests(unittest.TestCase):
    def test_p2p_layers_are_global_then_user(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            specs = persona_layer_specs(
                workspace_root=root,
                user_root=root / "p2p_u",
                chat_kind="p2p",
                chat_id=None,
            )
            self.assertEqual([scope for scope, _ in specs], ["global", "user"])

    def test_group_layers_exclude_actor_user_persona(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            specs = persona_layer_specs(
                workspace_root=root,
                user_root=root / "group_g1" / "user_u",
                chat_kind="group",
                chat_id="g1",
            )
            self.assertEqual([scope for scope, _ in specs], ["global", "group"])
            with self.assertRaises(ValueError):
                persona_path_for_scope(
                    "user",
                    workspace_root=root,
                    user_root=root / "group_g1" / "user_u",
                    chat_kind="group",
                    chat_id="g1",
                )


class PersonaToolHandlerTests(unittest.TestCase):
    def test_owner_sets_and_shows_p2p_global_then_user(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = _p2p_service(Path(tmp))
            with bind_workspace_service(service), bind_caller_role("owner"):
                persona_tools._handler_persona_set(
                    {"text": "默认友善", "scope": "global"}
                )
                persona_tools._handler_persona_set({"text": "说话简洁专业"})
                summary, outputs, _ = persona_tools._handler_persona_show({})
            self.assertIn("默认友善", summary)
            self.assertIn("说话简洁专业", summary)
            self.assertLess(summary.index("默认友善"), summary.index("说话简洁专业"))
            self.assertEqual(outputs, [])
            state = service.resolve_persistent_state()
            self.assertEqual(state.persona_snapshot("user"), "说话简洁专业\n")

    def test_owner_group_defaults_to_group_and_keeps_global_layer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = _group_service(Path(tmp))
            with bind_workspace_service(service), bind_caller_role("owner"):
                persona_tools._handler_persona_set(
                    {"text": "全局基础", "scope": "global"}
                )
                summary, outputs, _ = persona_tools._handler_persona_set(
                    {"text": "直接作为莫宁本人说话"}
                )
                shown, _, _ = persona_tools._handler_persona_show({})
            self.assertEqual(summary, "已覆盖 group 层人格。")
            self.assertEqual(outputs, [])
            self.assertIn("全局基础", shown)
            self.assertIn("直接作为莫宁本人说话", shown)
            self.assertFalse((service.workspace.root / "PERSONA.md").exists())
            protected = list(
                Path(tmp).glob(
                    ".conversation-state/persistent/persona/group/*/PERSONA.md"
                )
            )
            self.assertEqual(len(protected), 1)
            self.assertEqual(protected[0].stat().st_mode & 0o777, 0o600)

    def test_all_persona_handlers_reject_non_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = _p2p_service(Path(tmp))
            with bind_workspace_service(service), bind_caller_role("user"):
                for handler, args in (
                    (persona_tools._handler_persona_show, {}),
                    (persona_tools._handler_persona_set, {"text": "不应成功"}),
                    (persona_tools._handler_persona_append, {"text": "不应成功"}),
                    (persona_tools._handler_persona_clear, {"confirm": True}),
                ):
                    with self.subTest(handler=handler.__name__), self.assertRaises(
                        PermissionError
                    ):
                        handler(args)

    def test_append_clear_and_confirm_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = _p2p_service(Path(tmp))
            with bind_workspace_service(service), bind_caller_role("owner"):
                persona_tools._handler_persona_append({"text": "口头禅：稳。"})
                self.assertIn(
                    "口头禅", service.resolve_persistent_state().persona_snapshot("user")
                )
                with self.assertRaises(ValueError):
                    persona_tools._handler_persona_clear({})
                persona_tools._handler_persona_clear({"confirm": True})
                self.assertEqual(
                    service.resolve_persistent_state().persona_layers(),
                    (),
                )


class PersonaCapabilityAndPermissionTests(unittest.TestCase):
    def test_tool_pack_exposes_owner_only_persona_tools(self) -> None:
        tools = tuple(discover_tools(tool_packs=("persona.manage",)))
        self.assertEqual(
            {tool.name for tool in tools},
            {"persona_show", "persona_set", "persona_append", "persona_clear"},
        )
        self.assertTrue(all(tool.requires_role == "owner" for tool in tools))

    def test_non_owner_schema_omits_every_persona_tool(self) -> None:
        tools = tuple(discover_tools(tool_packs=("persona.manage",)))
        runtime = AgentRuntime(
            llm=object(),
            tools=tools,
            tools_schema=tuple(build_openai_schema(tool) for tool in tools),
            runtime_config=SimpleNamespace(runtime=SimpleNamespace(max_tool_retries=1)),
        )

        def permission_filter(tool):
            if tool.requires_role is None or role_ge(Role.USER, tool.requires_role):
                return None
            return f"需要 {role_value(tool.requires_role)}"

        session = runtime.new_session(
            session_id="s1",
            system_baseline="baseline",
            permission_filter=permission_filter,
        )
        self.assertEqual(session.tools_schema, [])


if __name__ == "__main__":
    unittest.main()
