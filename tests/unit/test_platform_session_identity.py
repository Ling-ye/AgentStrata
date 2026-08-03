from __future__ import annotations

import io
import os
import shutil
import subprocess
import textwrap
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

from chatcopilot.botspec.cli import _render_cc_connect_config, main as bot_cli_main
from chatcopilot.platforms import router


REPO_ROOT = Path(__file__).resolve().parents[2]
SESSION_ENV_HOOK = REPO_ROOT / "deploy" / "wsl" / "_session_env.sh"
LINGYE_BOT_SPEC = REPO_ROOT / "bots" / "lingye-copilot-qq" / "bot.yaml"


class _CaptureStdout:
    def __init__(self) -> None:
        self.buffer = io.BytesIO()

    def write(self, value: str) -> int:
        data = value.encode("utf-8")
        self.buffer.write(data)
        return len(value)

    def flush(self) -> None:
        pass

    def text(self) -> str:
        return self.buffer.getvalue().decode("utf-8")


class PlatformSessionIdentityTests(unittest.TestCase):
    def test_qq_private_session_key_uses_second_segment_as_user_id(self) -> None:
        identity = router.parse_session_identity("qq", session_key="qq:user-001")

        self.assertEqual(identity.user_id, "user-001")
        self.assertIsNone(identity.chat_id)
        self.assertEqual(identity.chat_kind, "p2p")

    def test_qq_group_session_key_uses_group_and_user_segments(self) -> None:
        identity = router.parse_session_identity("qq", session_key="qq:group-001:user-001")

        self.assertEqual(identity.chat_id, "group-001")
        self.assertEqual(identity.user_id, "user-001")
        self.assertEqual(identity.chat_kind, "group")

    def test_explicit_hook_fields_win_over_session_key(self) -> None:
        identity = router.parse_session_identity(
            "qq",
            session_key="qq:group_from_key:user_from_key",
            hook_user_id="user_from_hook",
            hook_chat_id="chat_from_hook",
            hook_chat_kind="direct",
            hook_user_name="Lingye",
        )

        self.assertEqual(identity.user_id, "user_from_hook")
        self.assertEqual(identity.chat_id, "chat_from_hook")
        self.assertEqual(identity.chat_kind, "direct")
        self.assertEqual(identity.user_name, "Lingye")

    def test_default_parser_handles_feishu_three_segment_key(self) -> None:
        identity = router.parse_session_identity(
            "feishu",
            session_key="feishu:oc_chat:ou_user",
            hook_chat_kind="group",
        )

        self.assertEqual(identity.chat_id, "oc_chat")
        self.assertEqual(identity.user_id, "ou_user")
        self.assertEqual(identity.chat_kind, "group")

    def test_render_session_env_cli_outputs_shell_exports(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            bot_dir = base / "qq-bot"
            bot_dir.mkdir()
            (bot_dir / "persona.md").write_text("test bot\n", encoding="utf-8")
            bot_yaml = bot_dir / "bot.yaml"
            bot_yaml.write_text(
                textwrap.dedent(
                    """\
                    id: qq-bot
                    display_name: QQ Bot
                    platform:
                      type: qq
                      adapter: qq_acp
                    prompts:
                      persona: persona.md
                    tools:
                      packs: []
                    deploy:
                      target: wsl2
                      instance_id: qq-bot
                    """
                ),
                encoding="utf-8",
            )
            stdout = _CaptureStdout()

            with redirect_stdout(stdout):  # type: ignore[arg-type]
                code = bot_cli_main(
                    [
                        "render-session-env",
                        "--bot",
                        str(bot_yaml),
                        "--session-key",
                        "qq:user-001",
                    ]
                )

            self.assertEqual(code, 0)
            content = stdout.text()
            self.assertIn("export CHATCOPILOT_USER_ID=user-001", content)
            self.assertIn("export CHATCOPILOT_CHAT_ID=''", content)
            self.assertIn("export CHATCOPILOT_CHAT_KIND=p2p", content)

    def test_render_cc_config_refreshes_session_env_on_each_message(self) -> None:
        config = _render_cc_connect_config(
            "qq",
            {
                "CHATCOPILOT_HOME": "/opt/chatcopilot",
                "CHATCOPILOT_WORKSPACE_ROOT": "/tmp/workspaces",
                "CHATCOPILOT_LOG_DIR": "/tmp/logs",
                "CHATCOPILOT_CC_HOME": "/tmp/cc-home",
                "CHATCOPILOT_CC_PROJECT_NAME": "qq-bot",
                "CHATCOPILOT_INSTANCE_ID": "qq-bot",
                "CHATCOPILOT_BOT_SPEC": "/opt/chatcopilot/bots/qq-bot/bot.yaml",
                "QQ_ACCESS_TOKEN": "a" * 32,
            },
        )

        self.assertIn('event = "message.received"', config)
        self.assertIn('command = "/opt/chatcopilot/deploy/wsl/_session_env.sh"', config)
        self.assertIn("async = false", config)
        self.assertIn('event = "session.started"', config)

    @unittest.skipUnless(shutil.which("bash"), "requires bash")
    def test_session_hook_uses_deploy_python_when_home_is_isolated(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            isolated_home = base / "cc-home"
            isolated_home.mkdir()
            runtime_env = base / "runtime.env"
            runtime_env.write_text("", encoding="utf-8")
            user_id = f"hook-test-{os.getpid()}-{base.name}"
            session_key = f"qq:{user_id}"
            target = Path("/tmp") / f"cc-sess-{session_key}.env"
            env = {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": str(isolated_home),
                "CHATCOPILOT_INSTANCE_ID": "lingye-copilot-qq",
                "CHATCOPILOT_HOME": str(REPO_ROOT),
                "CHATCOPILOT_BOT_SPEC": str(LINGYE_BOT_SPEC),
                "CHATCOPILOT_ENV_FILE": str(runtime_env),
                "CC_HOOK_SESSION_KEY": session_key,
            }

            try:
                result = subprocess.run(
                    ["bash", str(SESSION_ENV_HOOK)],
                    cwd=REPO_ROOT,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )

                self.assertEqual(result.returncode, 0, result.stderr)
                content = target.read_text(encoding="utf-8")
                self.assertIn(f"export CHATCOPILOT_USER_ID={user_id}", content)
                self.assertIn("export CHATCOPILOT_CHAT_KIND=p2p", content)
                self.assertNotIn("fallback to explicit hook fields", result.stderr)
            finally:
                target.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
