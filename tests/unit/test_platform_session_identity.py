from __future__ import annotations

import io
import hashlib
import json
import os
import shutil
import subprocess
import sys
import textwrap
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

from chatcopilot.botspec.cli import _render_cc_connect_config, main as bot_cli_main
from chatcopilot.platforms import router


REPO_ROOT = Path(__file__).resolve().parents[2]
SESSION_ENV_HOOK = REPO_ROOT / "deploy" / "wsl" / "_session_env.sh"
WSL_ENV_LOADER = REPO_ROOT / "deploy" / "wsl" / "_load_env.sh"
LINGYE_BOT_SPEC = REPO_ROOT / "bots" / "lingye-copilot-qq" / "bot.yaml"


def _session_env_target(directory: Path, session_key: str) -> Path:
    digest = hashlib.sha256(session_key.encode("utf-8")).hexdigest()
    return directory / f"cc-sess-{digest}.env"


def _run_session_hook(
    *,
    base: Path,
    session_key: str,
    event: str = "message.received",
    user_id: str = "20002",
    user_name: str = "Test User",
    content: str = "hello",
    session_env_dir: Path | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    isolated_home = base / "cc-home"
    isolated_home.mkdir(exist_ok=True)
    runtime_env = base / "runtime.env"
    runtime_env.write_text("", encoding="utf-8")
    private_dir = session_env_dir or isolated_home / "session-env"
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(isolated_home),
        "CHATCOPILOT_INSTANCE_ID": "lingye-copilot-qq",
        "CHATCOPILOT_HOME": str(REPO_ROOT),
        "CHATCOPILOT_BOT_SPEC": str(LINGYE_BOT_SPEC),
        "CHATCOPILOT_ENV_FILE": str(runtime_env),
        "CHATCOPILOT_SESSION_ENV_DIR": str(private_dir),
        "CC_HOOK_EVENT": event,
        "CC_HOOK_SESSION_KEY": session_key,
        "CC_HOOK_PLATFORM": "qq",
        "CC_HOOK_USER_ID": user_id,
        "CC_HOOK_USER_NAME": user_name,
        "CC_HOOK_CONTENT": content,
    }
    result = subprocess.run(
        ["bash", str(SESSION_ENV_HOOK)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    return result, _session_env_target(private_dir, session_key)


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

    def test_qq_shared_group_session_key_is_conversation_scoped(self) -> None:
        identity = router.parse_session_identity(
            "qq",
            session_key="qq:g:30003",
            hook_user_id="20002",
            hook_user_name="Untrusted Hook Name",
        )

        self.assertEqual(identity.chat_id, "30003")
        self.assertEqual(identity.chat_kind, "group")
        self.assertIsNone(identity.user_id)
        self.assertIsNone(identity.user_name)

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
        self.assertIn("share_session_in_channel = true", config)
        self.assertIn("inject_sender = true", config)
        self.assertIn('CHATCOPILOT_GROUP_CONVERSATION_SCOPE = "chat"', config)
        self.assertIn('CHATCOPILOT_SESSION_ENV_DIR = "/tmp/cc-home/session-env"', config)
        instant_reply = config.split("[instant_reply]", 1)[1].split("\n[", 1)[0]
        self.assertIn("enabled = false", instant_reply)
        self.assertNotIn("content =", instant_reply)
        self.assertNotIn("喵喵喵，正在分析中...", config)

    @unittest.skipUnless(shutil.which("bash"), "requires bash")
    def test_deploy_paths_ignore_cc_connect_isolated_home(self) -> None:
        import pwd

        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            isolated_home = base / "cc-home"
            isolated_home.mkdir()
            bot_yaml = base / "bot.yaml"
            bot_yaml.write_text(
                textwrap.dedent(
                    """\
                    id: qq-bot
                    display_name: QQ Bot
                    deploy:
                      target: wsl2
                      instance_id: qq-bot
                      wsl_home: ~/ChatCopilot-qq-bot
                      workspace_root: ~/chatcopilot-workspaces/qq-bot
                      env_file: ~/.chatcopilot-qq-bot.env
                      cc_connect_config_dir: ~/.chatcopilot-runtime/qq-bot/.cc-connect
                    """
                ),
                encoding="utf-8",
            )
            env = {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": str(isolated_home),
                "CHATCOPILOT_INSTANCE_ID": "qq-bot",
                "CHATCOPILOT_BOT_SPEC": str(bot_yaml),
            }

            result = subprocess.run(
                [
                    "bash",
                    "-c",
                    'source "$1"; ccp_apply_bot_deploy_config; '
                    'printf "%s\\n%s\\n%s\\n" "$CHATCOPILOT_HOME" '
                    '"$CHATCOPILOT_WORKSPACE_ROOT" "$CHATCOPILOT_CC_HOME"',
                    "bash",
                    str(WSL_ENV_LOADER),
                ],
                cwd=REPO_ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            account_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
            self.assertEqual(
                result.stdout.splitlines(),
                [
                    str(account_home / "ChatCopilot-qq-bot"),
                    str(account_home / "chatcopilot-workspaces/qq-bot"),
                    str(account_home / ".chatcopilot-runtime/qq-bot"),
                ],
            )
            self.assertNotIn(str(isolated_home), result.stdout)

    @unittest.skipUnless(shutil.which("bash"), "requires bash")
    def test_session_hook_uses_deploy_python_when_home_is_isolated(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            user_id = f"hook-test-{os.getpid()}-{base.name}"
            session_key = f"qq:{user_id}"
            result, target = _run_session_hook(
                base=base,
                session_key=session_key,
                user_id=user_id,
                content="  hello from hook  ",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(target.read_text(encoding="utf-8"))
            values = payload["identity"]
            attestation = payload["attestations"][0]
            self.assertEqual(payload["schema_version"], 2)
            self.assertEqual(
                payload["session_key_sha256"],
                hashlib.sha256(session_key.encode("utf-8")).hexdigest(),
            )
            self.assertEqual(values["CHATCOPILOT_USER_ID"], user_id)
            self.assertEqual(values["CHATCOPILOT_CHAT_KIND"], "p2p")
            self.assertEqual(attestation["event"], "message.received")
            self.assertEqual(attestation["transport_user_id"], user_id)
            self.assertEqual(
                attestation["content_sha256"],
                hashlib.sha256(b"hello from hook").hexdigest(),
            )
            self.assertRegex(attestation["record_id"], r"^[0-9a-f]{32}$")
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            self.assertEqual(target.parent.stat().st_mode & 0o777, 0o700)
            self.assertNotIn(session_key, target.name)
            self.assertNotIn("fallback to explicit hook fields", result.stderr)

    @unittest.skipUnless(shutil.which("bash"), "requires bash")
    def test_session_started_does_not_overwrite_message_attestation(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            session_key = "qq:g:30003"
            first, target = _run_session_hook(
                base=base,
                session_key=session_key,
                user_id="20002",
                content="first message",
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            before = target.read_bytes()

            started, same_target = _run_session_hook(
                base=base,
                session_key=session_key,
                event="session.started",
                user_id="29999",
                content="replacement",
            )

            self.assertEqual(started.returncode, 0, started.stderr)
            self.assertEqual(same_target, target)
            self.assertEqual(target.read_bytes(), before)

    @unittest.skipUnless(shutil.which("bash"), "requires bash")
    def test_session_hook_rejects_symlink_preoccupation_and_weak_modes(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            private_dir = base / "private"
            private_dir.mkdir(mode=0o700)
            session_key = "qq:g:30003"
            target = _session_env_target(private_dir, session_key)
            sentinel = base / "sentinel"
            sentinel.write_text("do-not-replace", encoding="utf-8")
            target.symlink_to(sentinel)

            symlink_result, _ = _run_session_hook(
                base=base,
                session_key=session_key,
                session_env_dir=private_dir,
            )
            self.assertNotEqual(symlink_result.returncode, 0)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "do-not-replace")
            self.assertTrue(target.is_symlink())

            target.unlink()
            target.write_text("{}\n", encoding="utf-8")
            target.chmod(0o644)
            weak_file_result, _ = _run_session_hook(
                base=base,
                session_key=session_key,
                session_env_dir=private_dir,
            )
            self.assertNotEqual(weak_file_result.returncode, 0)
            self.assertEqual(target.stat().st_mode & 0o777, 0o644)

            target.unlink()
            private_dir.chmod(0o755)
            weak_dir_result, _ = _run_session_hook(
                base=base,
                session_key=session_key,
                session_env_dir=private_dir,
            )
            self.assertNotEqual(weak_dir_result.returncode, 0)
            self.assertFalse(target.exists())

            private_dir.chmod(0o700)
            restored_result, _ = _run_session_hook(
                base=base,
                session_key=session_key,
                session_env_dir=private_dir,
            )
            self.assertEqual(restored_result.returncode, 0, restored_result.stderr)
            before = target.read_bytes()
            os.link(target, base / "session-env-hardlink")
            hardlink_result, _ = _run_session_hook(
                base=base,
                session_key=session_key,
                session_env_dir=private_dir,
            )
            self.assertNotEqual(hardlink_result.returncode, 0)
            self.assertEqual(target.read_bytes(), before)

    @unittest.skipUnless(shutil.which("bash"), "requires bash")
    def test_session_hook_never_executes_hook_text_and_hashes_path_segments(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            private_dir = base / "private"
            sentinel = base / "must-not-exist"
            session_key = "qq:../../outside"
            shell_text = f"--help\n$(touch {sentinel}) ; `touch {sentinel}`"

            result, target = _run_session_hook(
                base=base,
                session_key=session_key,
                user_id="20002",
                user_name=shell_text,
                content=shell_text,
                session_env_dir=private_dir,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(sentinel.exists())
            self.assertEqual(target.parent, private_dir)
            self.assertRegex(target.name, r"^cc-sess-[0-9a-f]{64}\.env$")
            payload = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(
                payload["attestations"][0]["content_sha256"],
                hashlib.sha256(shell_text.strip().encode("utf-8")).hexdigest(),
            )

            runtime_env = dict(os.environ)
            runtime_env["PYTHONPATH"] = str(REPO_ROOT / "src")
            loaded = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "chatcopilot",
                    "bot",
                    "exec-session-runtime",
                    "--bot",
                    str(LINGYE_BOT_SPEC),
                    "--session-env-dir",
                    str(private_dir),
                    "--session-key",
                    session_key,
                    "--",
                    "--help",
                ],
                cwd=REPO_ROOT,
                env=runtime_env,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(loaded.returncode, 0, loaded.stderr)
            self.assertIn("Run an AgentStrata bot", loaded.stdout)
            self.assertFalse(sentinel.exists())


if __name__ == "__main__":
    unittest.main()
