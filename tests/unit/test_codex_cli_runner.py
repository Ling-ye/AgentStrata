from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, mock

from chatcopilot.external_tools.codex_cli.command import (
    build_codex_command,
    build_codex_subprocess_env,
)


class CodexCommandTests(TestCase):
    def test_workspace_command_isolated_and_security_checked(self) -> None:
        with mock.patch(
            "chatcopilot.external_tools.codex_cli.command.shutil.which",
            return_value="/usr/bin/codex",
        ):
            command = build_codex_command(
                "codex exec --model {model} --cwd {workdir}",
                model="gpt-test",
                workdir=Path("/tmp/work"),
                network_access=True,
                web_search_mode="live",
                ephemeral=False,
                extra_config=("mcp_servers={}",),
            )

        self.assertEqual(command[:4], ["/usr/bin/codex", "exec", "--model", "gpt-test"])
        self.assertIn("--ignore-user-config", command)
        self.assertIn("workspace-write", command)
        self.assertIn("sandbox_workspace_write.network_access=true", command)
        self.assertIn('web_search="live"', command)
        self.assertIn('shell_environment_policy.inherit="none"', command)
        self.assertTrue(
            any('HOME = "/tmp/work"' in item for item in command),
            command,
        )
        self.assertIn("mcp_servers={}", command)

    def test_host_command_inherits_environment_and_user_config(self) -> None:
        with mock.patch(
            "chatcopilot.external_tools.codex_cli.command.shutil.which",
            return_value="/usr/bin/codex",
        ):
            command = build_codex_command(
                "codex exec --model {model} --cd {workdir}",
                model="gpt-test",
                workdir=Path("/tmp/source"),
                sandbox_mode="danger-full-access",
                network_access=True,
                web_search_mode="live",
                ephemeral=False,
                ignore_user_config=False,
                inherit_shell_environment=True,
            )

        self.assertIn("danger-full-access", command)
        self.assertIn('shell_environment_policy.inherit="all"', command)
        self.assertNotIn("--ignore-user-config", command)
        self.assertFalse(any("shell_environment_policy.set" in item for item in command))

    def test_template_rejects_security_flags_and_shell_injection(self) -> None:
        cases = (
            "codex exec --sandbox danger-full-access",
            "codex exec --config web_search=live",
            "codex exec; touch /tmp/pwned",
        )
        with mock.patch(
            "chatcopilot.external_tools.codex_cli.command.shutil.which",
            return_value="/usr/bin/codex",
        ):
            for template in cases:
                with self.subTest(template=template), self.assertRaises(RuntimeError):
                    build_codex_command(
                        template,
                        model="gpt-test",
                        workdir=Path("/tmp/work"),
                    )

    def test_missing_codex_reports_actionable_error(self) -> None:
        with mock.patch(
            "chatcopilot.external_tools.codex_cli.command.shutil.which",
            return_value=None,
        ), mock.patch(
            "chatcopilot.external_tools.codex_cli.command._codex_candidates",
            return_value=[],
        ):
            with self.assertRaisesRegex(FileNotFoundError, "Codex CLI"):
                build_codex_command(
                    "codex exec",
                    model="gpt-test",
                    workdir=Path("/tmp/work"),
                )


class CodexEnvironmentTests(TestCase):
    def test_member_environment_uses_prepared_isolated_home(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_home = root / "source-home"
            runtime_home = root / "runtime-home"
            source_home.mkdir()
            runtime_home.mkdir(mode=0o700)
            (source_home / "auth.json").write_text("auth", encoding="utf-8")
            (source_home / "config.toml").write_text("personal", encoding="utf-8")
            env = {
                "HOME": str(root),
                "PATH": "/usr/bin",
                "CODEX_HOME": str(source_home),
                "CHATCOPILOT_SECRET": "do-not-copy",
                "OPENAI_API_KEY": "do-not-copy",
                "CODEX_API_KEY": "allowed-auth",
            }
            with mock.patch.dict(os.environ, env, clear=True):
                isolated = build_codex_subprocess_env(
                    "/usr/bin/codex",
                    runtime_home=runtime_home,
                )

        self.assertEqual(isolated["CODEX_HOME"], str(runtime_home.resolve()))
        self.assertEqual(isolated["CODEX_SQLITE_HOME"], str(runtime_home.resolve()))
        self.assertNotIn("CODEX_API_KEY", isolated)
        self.assertNotIn("CHATCOPILOT_SECRET", isolated)
        self.assertNotIn("OPENAI_API_KEY", isolated)
        self.assertFalse((runtime_home / "auth.json").exists())
        self.assertFalse((runtime_home / "config.toml").exists())

    def test_isolated_environment_never_reads_personal_or_dedicated_auth(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            personal = root / "personal"
            runtime_home = root / "runtime"
            personal.mkdir()
            runtime_home.mkdir(mode=0o700)
            (personal / "auth.json").write_text("personal", encoding="utf-8")
            with mock.patch.dict(
                os.environ,
                {"CODEX_HOME": str(personal), "PATH": "/usr/bin"},
                clear=True,
            ):
                isolated = build_codex_subprocess_env(
                    "/usr/bin/codex",
                    runtime_home=runtime_home,
                )

        self.assertEqual(isolated["CODEX_HOME"], str(runtime_home.resolve()))
        self.assertFalse((runtime_home / "auth.json").exists())

    def test_isolated_environment_rejects_unsafe_runtime_home(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            permissive = root / "permissive"
            permissive.mkdir(mode=0o755)
            target = root / "target"
            target.mkdir(mode=0o700)
            symlink = root / "symlink"
            symlink.symlink_to(target, target_is_directory=True)

            for runtime_home in (permissive, symlink, root / "missing"):
                with self.subTest(runtime_home=runtime_home), self.assertRaises(
                    RuntimeError
                ):
                    build_codex_subprocess_env(
                        "/usr/bin/codex",
                        runtime_home=runtime_home,
                    )

    def test_host_environment_is_fully_inherited(self) -> None:
        env = {
            "HOME": "/srv/owner",
            "CODEX_HOME": "/srv/owner/.codex",
            "PERSONAL_MCP_TOKEN": "kept",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            inherited = build_codex_subprocess_env(
                "/usr/bin/codex",
                inherit_all=True,
            )

        self.assertEqual(inherited, env)

    def test_full_inheritance_rejects_isolated_runtime_home(self) -> None:
        with self.assertRaises(ValueError):
            build_codex_subprocess_env(
                "/usr/bin/codex",
                runtime_home=Path("/tmp/codex-home"),
                inherit_all=True,
            )
