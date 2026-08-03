from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from chatcopilot.agent.persona import (
    PERSONA_FILENAME,
    merge_persona_layers,
    persona_layer_specs,
    persona_path_for_scope,
)
from chatcopilot.agent.runtime import AgentRuntime
from chatcopilot.agent.tools.builtin import persona_tools
from chatcopilot.agent.tools.registry import discover_tools
from chatcopilot.agent.tools.workspace_context import bind_workspace_service
from chatcopilot.contracts import Role, role_ge, role_value
from chatcopilot.core.caller_context import bind_caller_role
from chatcopilot.external_tools.shared.tool_spec import build_openai_schema
from chatcopilot.middleware.runtime.workspace import Workspace
from types import SimpleNamespace


class _WorkspaceService:
    def __init__(self, *, workspace_root: Path, user_root: Path, chat_kind, chat_id, user_id):
        self.root = workspace_root
        self.workspace = Workspace(
            root=user_root,
            chat_kind=chat_kind,
            chat_id=chat_id,
            user_id=user_id,
        ).ensure()

    def resolve_workspace(self, *, create: bool = True) -> Workspace:
        return self.workspace

    def resolve_workspace_root(self, workspace=None) -> Path:
        return self.root

    def cleanup_workspace(self, workspace) -> None:
        return None

    def describe_workspace(self, workspace) -> str:
        return f"workspace={workspace.root}"

    def list_workspace_inventories(self, root: Path) -> list:
        return []


def _p2p_service(root: Path) -> _WorkspaceService:
    return _WorkspaceService(
        workspace_root=root,
        user_root=root / "p2p_user-001",
        chat_kind="p2p",
        chat_id=None,
        user_id="user-001",
    )


def _group_service(root: Path) -> _WorkspaceService:
    return _WorkspaceService(
        workspace_root=root,
        user_root=root / "group_g1" / "user_user-001",
        chat_kind="group",
        chat_id="g1",
        user_id="user-001",
    )


class PersonaLayerTests(unittest.TestCase):
    def test_p2p_layers_are_global_then_user(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            specs = persona_layer_specs(
                workspace_root=root,
                user_root=root / "p2p_u",
                chat_kind="p2p",
                chat_id=None,
            )
            scopes = [scope for scope, _ in specs]
            self.assertEqual(scopes, ["global", "user"])
            self.assertEqual(specs[0][1], root / PERSONA_FILENAME)
            self.assertEqual(specs[1][1], root / "p2p_u" / PERSONA_FILENAME)

    def test_group_layers_are_global_group_user(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            user_root = root / "group_g1" / "user_u"
            specs = persona_layer_specs(
                workspace_root=root,
                user_root=user_root,
                chat_kind="group",
                chat_id="g1",
            )
            scopes = [scope for scope, _ in specs]
            self.assertEqual(scopes, ["global", "group", "user"])
            self.assertEqual(specs[1][1], root / "group_g1" / PERSONA_FILENAME)

    def test_group_scope_rejected_in_p2p(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(ValueError):
                persona_path_for_scope(
                    "group",
                    workspace_root=root,
                    user_root=root / "p2p_u",
                    chat_kind="p2p",
                    chat_id=None,
                )

    def test_merge_orders_layers_with_labels(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            g = root / PERSONA_FILENAME
            u = root / "p2p_u" / PERSONA_FILENAME
            g.write_text("全局基础人格", encoding="utf-8")
            u.parent.mkdir(parents=True, exist_ok=True)
            u.write_text("对该用户毒舌", encoding="utf-8")
            merged = merge_persona_layers(
                persona_layer_specs(
                    workspace_root=root,
                    user_root=root / "p2p_u",
                    chat_kind="p2p",
                    chat_id=None,
                )
            )
            self.assertIn("全局基础人格", merged)
            self.assertIn("对该用户毒舌", merged)
            # 全局层应排在专属层之前
            self.assertLess(merged.index("全局基础人格"), merged.index("对该用户毒舌"))


class PersonaToolHandlerTests(unittest.TestCase):
    def test_set_then_show_user_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with bind_workspace_service(_p2p_service(root)):
                persona_tools._handler_persona_set({"text": "说话简洁专业", "scope": "user"})
                summary, outputs, _ = persona_tools._handler_persona_show({})
            self.assertIn("说话简洁专业", summary)
            self.assertTrue((root / "p2p_user-001" / PERSONA_FILENAME).is_file())

    def test_set_global_scope_targets_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with bind_workspace_service(_p2p_service(root)), bind_caller_role("owner"):
                persona_tools._handler_persona_set({"text": "默认友善", "scope": "global"})
            self.assertTrue((root / PERSONA_FILENAME).is_file())
            self.assertIn("默认友善", (root / PERSONA_FILENAME).read_text(encoding="utf-8"))

    def test_non_owner_cannot_set_global_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with bind_workspace_service(_p2p_service(root)), bind_caller_role("user"):
                with self.assertRaises(PermissionError):
                    persona_tools._handler_persona_set({"text": "不该成功", "scope": "global"})

    def test_non_owner_can_set_user_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with bind_workspace_service(_p2p_service(root)), bind_caller_role("user"):
                summary, _, _ = persona_tools._handler_persona_set({"text": "我的个性", "scope": "user"})
            self.assertIn("已覆盖 user 层个性", summary)

    def test_non_owner_can_set_group_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with bind_workspace_service(_group_service(root)), bind_caller_role("user"):
                summary, _, _ = persona_tools._handler_persona_set({"text": "群个性", "scope": "group"})
            self.assertIn("已覆盖 group 层个性", summary)

    def test_group_scope_writes_group_layer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with bind_workspace_service(_group_service(root)):
                persona_tools._handler_persona_set({"text": "群里正式", "scope": "group"})
            self.assertTrue((root / "group_g1" / PERSONA_FILENAME).is_file())

    def test_group_default_scope_writes_group_layer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with bind_workspace_service(_group_service(root)):
                summary, outputs, _ = persona_tools._handler_persona_set({"text": "我是卡提西亚"})
            self.assertIn("已覆盖 group 层个性", summary)
            self.assertTrue((root / "group_g1" / PERSONA_FILENAME).is_file())
            self.assertFalse((root / "group_g1" / "user_user-001" / PERSONA_FILENAME).is_file())
            self.assertIn(str(root / "group_g1" / PERSONA_FILENAME), outputs)

    def test_p2p_default_scope_still_writes_user_layer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with bind_workspace_service(_p2p_service(root)):
                summary, outputs, _ = persona_tools._handler_persona_set({"text": "说话简洁专业"})
            self.assertIn("已覆盖 user 层个性", summary)
            self.assertTrue((root / "p2p_user-001" / PERSONA_FILENAME).is_file())
            self.assertIn(str(root / "p2p_user-001" / PERSONA_FILENAME), outputs)

    def test_append_and_clear(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "p2p_user-001" / PERSONA_FILENAME
            with bind_workspace_service(_p2p_service(root)):
                persona_tools._handler_persona_append({"text": "口头禅：稳。", "scope": "user"})
                self.assertIn("口头禅", target.read_text(encoding="utf-8"))
                persona_tools._handler_persona_clear({"scope": "user", "confirm": True})
                self.assertNotIn("口头禅", target.read_text(encoding="utf-8"))

    def test_clear_requires_confirm(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with bind_workspace_service(_p2p_service(root)):
                with self.assertRaises(ValueError):
                    persona_tools._handler_persona_clear({"scope": "user"})


class PersonaCapabilityAndPermissionTests(unittest.TestCase):
    def test_tool_pack_exposes_persona_tools(self) -> None:
        names = {tool.name for tool in discover_tools(tool_packs=("persona.manage",))}
        self.assertEqual(
            names,
            {"persona_show", "persona_set", "persona_append", "persona_clear"},
        )

    def test_write_tools_have_no_role_gate_show_is_open(self) -> None:
        by_name = {tool.name: tool for tool in discover_tools(tool_packs=("persona.manage",))}
        for name in ("persona_show", "persona_set", "persona_append", "persona_clear"):
            self.assertIsNone(by_name[name].requires_role)

    def test_all_users_can_see_persona_write_tools(self) -> None:
        tools = tuple(discover_tools(tool_packs=("persona.manage",)))
        runtime = AgentRuntime(
            llm=object(),
            tools=tools,
            tools_schema=tuple(build_openai_schema(tool) for tool in tools),
            runtime_config=SimpleNamespace(runtime=SimpleNamespace(max_tool_retries=1)),
        )

        def _filter(role: Role):
            def _f(tool):
                if tool.requires_role is None or role_ge(role, tool.requires_role):
                    return None
                return f"需要 {role_value(tool.requires_role)}"

            return _f

        session = runtime.new_session(
            session_id="s1",
            system_baseline="baseline",
            permission_filter=_filter(Role.USER),
        )
        schema_names = {entry["function"]["name"] for entry in session.tools_schema}
        self.assertIn("persona_show", schema_names)
        self.assertIn("persona_set", schema_names)
        self.assertIn("persona_append", schema_names)
        self.assertIn("persona_clear", schema_names)


if __name__ == "__main__":
    unittest.main()
