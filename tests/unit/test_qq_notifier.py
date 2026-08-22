from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from chatcopilot.core.workspace_runtime import Workspace
from chatcopilot.platforms.qq.notifier import QQNotifyError, resolve_delivery_target, send_text_to_workspace


class QQNotifierTests(unittest.TestCase):
    def test_resolves_private_and_group_targets(self) -> None:
        private = Workspace(Path("."), "p2p", None, "10001")
        group = Workspace(Path("."), "group", "20002", "10001")
        self.assertEqual(resolve_delivery_target(private).receive_id_type, "user_id")
        self.assertEqual(resolve_delivery_target(private).receive_id, "10001")
        self.assertEqual(resolve_delivery_target(group).receive_id_type, "group_id")
        self.assertEqual(resolve_delivery_target(group).receive_id, "20002")

    def test_rejects_missing_identity(self) -> None:
        with self.assertRaises(QQNotifyError):
            resolve_delivery_target(Workspace(Path("."), "p2p", None, None))

    @mock.patch("chatcopilot.platforms.qq.notifier.sender.send_text_via_onebot")
    def test_sends_group_text_through_onebot(self, send: mock.Mock) -> None:
        send.return_value = "msg-1"
        workspace = Workspace(Path("."), "group", "20002", "10001")
        result = send_text_to_workspace(workspace, "done", timeout=12)
        send.assert_called_once_with(
            message_type="group",
            id_key="group_id",
            id_value="20002",
            text="done",
            timeout=12,
        )
        self.assertEqual(result.message_id, "msg-1")

    @mock.patch("chatcopilot.platforms.qq.notifier.sender.send_text_via_onebot")
    def test_splits_long_notifications(self, send: mock.Mock) -> None:
        send.side_effect = ["msg-1", "msg-2"]
        workspace = Workspace(Path("."), "p2p", None, "10001")
        result = send_text_to_workspace(workspace, "x" * 4000)
        self.assertEqual(send.call_count, 2)
        self.assertEqual(result.message_id, "msg-2")


if __name__ == "__main__":
    unittest.main()
