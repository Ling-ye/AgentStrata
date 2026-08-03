from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from chatcopilot.middleware.acp import agent_bridge
from chatcopilot.middleware.runtime.workspace import Workspace


class AcpSessionIdentityRefreshTests(unittest.TestCase):
    def test_group_session_env_switches_current_user_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current = Workspace(
                root=root / "group_g1" / "user_u1",
                chat_kind="group",
                chat_id="g1",
                user_id="u1",
                user_name="User One",
            ).ensure()
            env_file = root / "cc-sess-qq.env"
            env_file.write_text(
                "\n".join(
                    [
                        "export CHATCOPILOT_USER_ID=u2",
                        "export CHATCOPILOT_CHAT_ID=g1",
                        "export CHATCOPILOT_CHAT_KIND=group",
                        "export CHATCOPILOT_USER_NAME='User Two'",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            with patch.object(agent_bridge, "_session_env_path", return_value=env_file):
                latest = agent_bridge._latest_workspace_from_session_env(current, platform_type="qq")

            self.assertIsNotNone(latest)
            assert latest is not None
            self.assertEqual(latest.chat_kind, "group")
            self.assertEqual(latest.chat_id, "g1")
            self.assertEqual(latest.user_id, "u2")
            self.assertEqual(latest.user_name, "User Two")
            self.assertEqual(latest.root.resolve(), (root / "group_g1" / "user_u2").resolve())

    def test_same_session_env_does_not_request_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current = Workspace(
                root=root / "group_g1" / "user_u1",
                chat_kind="group",
                chat_id="g1",
                user_id="u1",
                user_name="User One",
            ).ensure()
            env_file = root / "cc-sess-qq.env"
            env_file.write_text(
                "\n".join(
                    [
                        "export CHATCOPILOT_USER_ID=u1",
                        "export CHATCOPILOT_CHAT_ID=g1",
                        "export CHATCOPILOT_CHAT_KIND=group",
                        "export CHATCOPILOT_USER_NAME='User One'",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            with patch.object(agent_bridge, "_session_env_path", return_value=env_file):
                latest = agent_bridge._latest_workspace_from_session_env(current, platform_type="qq")

            self.assertIsNone(latest)


if __name__ == "__main__":
    unittest.main()
