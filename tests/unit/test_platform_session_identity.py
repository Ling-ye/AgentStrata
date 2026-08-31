from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

from chatcopilot.botspec.cli import main as bot_cli_main
from chatcopilot.platforms import router


REPO_ROOT = Path(__file__).resolve().parents[2]
LINGYE_BOT_SPEC = REPO_ROOT / "bots" / "lingye-copilot-qq" / "bot.yaml"


def _run_cli(argv: list[str]) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = bot_cli_main(argv)
    return code, stdout.getvalue(), stderr.getvalue()


class PlatformSessionIdentityTests(unittest.TestCase):
    def test_feishu_three_segment_session_key_is_still_supported(self) -> None:
        identity = router.parse_session_identity(
            "feishu",
            session_key="feishu:oc_chat:ou_user",
            hook_chat_kind="group",
        )

        self.assertEqual(identity.chat_id, "oc_chat")
        self.assertEqual(identity.user_id, "ou_user")
        self.assertEqual(identity.chat_kind, "group")

    def test_feishu_explicit_hook_fields_win_over_session_key(self) -> None:
        identity = router.parse_session_identity(
            "feishu",
            session_key="feishu:chat_from_key:user_from_key",
            hook_user_id="user_from_hook",
            hook_chat_id="chat_from_hook",
            hook_chat_kind="direct",
            hook_user_name="Display Name",
        )

        self.assertEqual(identity.user_id, "user_from_hook")
        self.assertEqual(identity.chat_id, "chat_from_hook")
        self.assertEqual(identity.chat_kind, "direct")
        self.assertEqual(identity.user_name, "Display Name")

    def test_gateway_qq_rejects_cc_connect_config_render(self) -> None:
        with TemporaryDirectory() as tmp:
            target = Path(tmp) / "config.toml"

            code, stdout, stderr = _run_cli(
                [
                    "render-cc-config",
                    "--bot",
                    str(LINGYE_BOT_SPEC),
                    "--out",
                    str(target),
                ]
            )

            self.assertEqual(code, 2)
            self.assertIn("qq_gateway_has_no_cc_connect_config", stdout)
            self.assertEqual(stderr, "")
            self.assertFalse(target.exists())

    def test_gateway_qq_rejects_session_env_render_before_writing_state(self) -> None:
        with TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "session-env"

            code, stdout, stderr = _run_cli(
                [
                    "render-session-env",
                    "--bot",
                    str(LINGYE_BOT_SPEC),
                    "--session-key",
                    "qq:g:30003",
                    "--session-env-dir",
                    str(state_dir),
                ]
            )

            self.assertEqual(code, 2)
            self.assertEqual(stdout, "")
            self.assertIn("qq_gateway_has_no_session_env", stderr)
            self.assertFalse(state_dir.exists())

    def test_gateway_qq_rejects_session_runtime_before_reading_state(self) -> None:
        with TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "missing-session-env"

            code, stdout, stderr = _run_cli(
                [
                    "exec-session-runtime",
                    "--bot",
                    str(LINGYE_BOT_SPEC),
                    "--session-env-dir",
                    str(state_dir),
                    "--session-key",
                    "qq:g:30003",
                    "--",
                    "--help",
                ]
            )

            self.assertEqual(code, 2)
            self.assertEqual(stdout, "")
            self.assertIn("qq_gateway_has_no_session_runtime", stderr)
            self.assertFalse(state_dir.exists())


if __name__ == "__main__":
    unittest.main()
