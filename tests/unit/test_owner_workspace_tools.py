"""Regression tests for Owner-only cross-workspace management tools."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from chatcopilot.middleware.access_control import Role
from chatcopilot.agent.tools.executor import ToolExecutor
from chatcopilot.middleware.runtime.workspace import Workspace, list_workspace_inventories, persist_workspace_identity
from chatcopilot.middleware.runtime.workspace.service import MiddlewareWorkspaceService
from chatcopilot.agent.tools.builtin.workspace_tools import TOOLS
from chatcopilot.middleware.acp.meta_commands import (
    _format_owner_global_workspace_status,
    _should_handle_owner_global_workspace_query,
)

_WS_SERVICE = MiddlewareWorkspaceService()


class OwnerWorkspaceInventoryTests(unittest.TestCase):
    def test_inventory_detects_p2p_and_group_user_workspaces(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            p2p = root / "p2p_ou_owner" / "downloads"
            group = root / "group_oc_team" / "user_ou_member" / "results"
            p2p.mkdir(parents=True)
            group.mkdir(parents=True)
            (p2p / "a.csv").write_text("a", encoding="utf-8")
            (group / "report.md").write_text("report", encoding="utf-8")
            persist_workspace_identity(
                Workspace(
                    root=root / "p2p_ou_owner",
                    chat_kind="p2p",
                    chat_id=None,
                    user_id="ou_owner",
                    user_name="Owner Name",
                )
            )

            items = list_workspace_inventories(root)

        by_path = {item.relative_path: item for item in items}
        self.assertIn("p2p_ou_owner", by_path)
        self.assertIn(str(Path("group_oc_team") / "user_ou_member"), by_path)
        self.assertEqual(by_path["p2p_ou_owner"].user_id, "ou_owner")
        self.assertEqual(by_path["p2p_ou_owner"].user_name, "Owner Name")
        self.assertEqual(by_path[str(Path("group_oc_team") / "user_ou_member")].chat_id, "oc_team")
        self.assertEqual(by_path[str(Path("group_oc_team") / "user_ou_member")].user_id, "ou_member")


class OwnerWorkspaceToolTests(unittest.TestCase):
    def test_user_cannot_call_owner_list_workspaces(self) -> None:
        result = ToolExecutor(tools=TOOLS, workspace_service=_WS_SERVICE).execute(
            "owner_list_workspaces",
            {},
            role=Role.USER,
        )

        self.assertFalse(result.ok)
        self.assertIn("需要 owner", result.error or "")

    def test_owner_can_list_workspaces_with_plain_user_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "p2p_ou_visible"
            downloads = workspace / "downloads"
            downloads.mkdir(parents=True)
            (downloads / "data.csv").write_text("x", encoding="utf-8")
            persist_workspace_identity(
                Workspace(
                    root=workspace,
                    chat_kind="p2p",
                    chat_id=None,
                    user_id="ou_visible",
                    user_name="Visible User",
                )
            )

            with mock.patch.dict(
                "os.environ",
                {"CHATCOPILOT_WORKSPACE_ROOT": str(root)},
                clear=False,
            ):
                result = ToolExecutor(tools=TOOLS, workspace_service=_WS_SERVICE).execute(
                    "owner_list_workspaces",
                    {},
                    role=Role.OWNER,
                )

        self.assertTrue(result.ok, result.error)
        self.assertIn("ou_visible", result.summary)
        self.assertIn("Visible User", result.summary)
        self.assertIn("p2p_ou_visible", result.summary)

    def test_owner_read_workspace_file_rejects_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "p2p_ou_visible"
            workspace.mkdir()
            (root / "secret.txt").write_text("secret", encoding="utf-8")

            with mock.patch.dict(
                "os.environ",
                {"CHATCOPILOT_WORKSPACE_ROOT": str(root)},
                clear=False,
            ):
                result = ToolExecutor(tools=TOOLS, workspace_service=_WS_SERVICE).execute(
                    "owner_read_workspace_file",
                    {
                        "workspace_path": "p2p_ou_visible",
                        "file_path": "../secret.txt",
                    },
                    role=Role.OWNER,
                )

        self.assertFalse(result.ok)
        self.assertIn("越出指定工作区", result.error or "")

    def test_owner_can_read_workspace_text_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "p2p_ou_visible"
            workspace.mkdir()
            (workspace / "MEMORY.md").write_text("# Memory\nhello", encoding="utf-8")

            with mock.patch.dict(
                "os.environ",
                {"CHATCOPILOT_WORKSPACE_ROOT": str(root)},
                clear=False,
            ):
                result = ToolExecutor(tools=TOOLS, workspace_service=_WS_SERVICE).execute(
                    "owner_read_workspace_file",
                    {
                        "workspace_path": "p2p_ou_visible",
                        "file_path": "MEMORY.md",
                    },
                    role=Role.OWNER,
                )

        self.assertTrue(result.ok, result.error)
        self.assertIn("hello", result.summary)


class OwnerWorkspaceShortcutTests(unittest.TestCase):
    def test_owner_global_workspace_status_counts_unique_users(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            owner_a = root / "group_oc_a" / "user_ou_owner"
            owner_b = root / "group_oc_b" / "user_ou_owner"
            member = root / "group_oc_a" / "user_ou_member"
            for workspace in (owner_a, owner_b, member):
                (workspace / "downloads").mkdir(parents=True)
            (owner_a / "downloads" / "a.csv").write_text("a", encoding="utf-8")
            (member / "downloads" / "b.csv").write_text("b", encoding="utf-8")
            persist_workspace_identity(
                Workspace(
                    root=owner_a,
                    chat_kind="group",
                    chat_id="oc_a",
                    user_id="ou_owner",
                    user_name="Owner Name",
                )
            )
            persist_workspace_identity(
                Workspace(
                    root=owner_b,
                    chat_kind="group",
                    chat_id="oc_b",
                    user_id="ou_owner",
                    user_name="Owner Name",
                )
            )
            persist_workspace_identity(
                Workspace(
                    root=member,
                    chat_kind="group",
                    chat_id="oc_a",
                    user_id="ou_member",
                    user_name="Member Name",
                )
            )

            with mock.patch.dict(
                "os.environ",
                {"CHATCOPILOT_WORKSPACE_ROOT": str(root)},
                clear=False,
            ):
                text = _format_owner_global_workspace_status(
                    Workspace(
                        root=owner_a,
                        chat_kind="group",
                        chat_id="oc_a",
                        user_id="ou_owner",
                        user_name="Owner Name",
                    )
                )

        self.assertIn("当前一共有 2 个明确用户使用过机器人", text)
        self.assertIn("已识别工作区 3 个", text)
        self.assertIn("user_id=ou_owner name=Owner Name workspaces=2", text)
        self.assertIn("user_id=ou_member name=Member Name workspaces=1", text)

    def test_global_workspace_shortcut_is_owner_only(self) -> None:
        owner_session = SimpleNamespace(role=Role.OWNER)
        user_session = SimpleNamespace(role=Role.USER)

        self.assertTrue(
            _should_handle_owner_global_workspace_query(
                owner_session,
                "告诉我，现在一共有几个用户使用了",
            )
        )
        self.assertFalse(
            _should_handle_owner_global_workspace_query(
                user_session,
                "告诉我，现在一共有几个用户使用了",
            )
        )


if __name__ == "__main__":
    unittest.main()
