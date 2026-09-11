"""ACP-owned QQ admission and post-admission access projection tests."""

from __future__ import annotations

import textwrap
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from chatcopilot.botspec.loader import load_botspec, validate_botspec

from chatcopilot.core.allowlists import AllowlistConfigError, parse_numeric_allowlist
from chatcopilot.middleware.access_control import Role, resolve_role
from chatcopilot.middleware.acp.admission import AdmissionDecision, evaluate_admission
from chatcopilot.middleware.acp.meta_commands import _handle_owner_runtime_info_query


def _write_bot_with_access(base: Path, access_block: str) -> Path:
    bot_dir = base / "test-bot"
    bot_dir.mkdir(parents=True, exist_ok=True)
    (bot_dir / "system.md").write_text("# 测试机器人\n", encoding="utf-8")
    yaml_text = textwrap.dedent(
        """\
        id: test-bot
        display_name: 测试机器人
        platform:
          type: qq
          adapter: qq_acp
        prompts:
          schema_version: 2
          identity: system.md
          response_style: system.md
        tools:
          packs: []
        deploy:
          target: wsl2
        """
    ) + access_block
    bot_yaml = bot_dir / "bot.yaml"
    bot_yaml.write_text(yaml_text, encoding="utf-8")
    return bot_yaml


class AccessSpecLoaderTests(unittest.TestCase):
    def test_old_access_values_require_migration(self) -> None:
        for value in ("true", "false", "invalid", ""):
            with self.subTest(value=value), TemporaryDirectory() as tmp:
                spec = load_botspec(
                    _write_bot_with_access(
                        Path(tmp), f"access:\n  owner_only_project_access: {value}\n"
                    )
                )
                errors = [issue for issue in validate_botspec(spec) if issue.level == "error"]
                self.assertTrue(
                    any(
                        issue.field == "access" and "Owner/member" in issue.message
                        for issue in errors
                    )
                )

    def test_missing_access_has_no_legacy_projection(self) -> None:
        with TemporaryDirectory() as tmp:
            spec = load_botspec(_write_bot_with_access(Path(tmp), ""))
        self.assertFalse(hasattr(spec, "access"))

    def test_removed_admission_fields_are_validation_errors(self) -> None:
        removed = (
            "private_require_whitelist",
            "group_require_whitelist",
            "group_require_mention",
            "whitelist_env",
            "group_whitelist_env",
            "enabled",
        )
        for field in removed:
            value = "QQ_ALLOW_FROM" if field == "whitelist_env" else "QQ_ALLOW_GROUPS"
            if field not in {"whitelist_env", "group_whitelist_env"}:
                value = "true"
            block = f"access:\n  {field}: {value}\n"
            with self.subTest(field=field), TemporaryDirectory() as tmp:
                spec = load_botspec(_write_bot_with_access(Path(tmp), block))
                errors = [issue for issue in validate_botspec(spec) if issue.level == "error"]
            self.assertTrue(any(issue.field == "access" for issue in errors))


class StrictAllowlistTests(unittest.TestCase):
    def test_missing_and_empty_grant_nothing(self) -> None:
        self.assertFalse(parse_numeric_allowlist(None, field="QQ_ALLOW_FROM").allow_all)
        self.assertEqual(
            parse_numeric_allowlist("", field="QQ_ALLOW_FROM").values,
            frozenset(),
        )

    def test_exact_wildcard_is_the_only_allow_all_value(self) -> None:
        self.assertTrue(
            parse_numeric_allowlist("*", field="QQ_ALLOW_FROM").allow_all
        )
        for raw in ("123,*", "*,123"):
            with self.subTest(raw=raw), self.assertRaises(AllowlistConfigError):
                parse_numeric_allowlist(raw, field="QQ_ALLOW_FROM")

    def test_numeric_values_are_trimmed_and_deduplicated(self) -> None:
        parsed = parse_numeric_allowlist(
            "10001, 20002,10001",
            field="QQ_ALLOW_FROM",
        )
        self.assertEqual(parsed.values, frozenset({"10001", "20002"}))

    def test_empty_tokens_and_non_numeric_values_fail_without_echoing_value(self) -> None:
        for raw in (
            "10001,",
            ",10001",
            "10001,,20002",
            "abc",
            "'10001'",
            "１０００１",
        ):
            with self.subTest(raw=raw), self.assertRaises(AllowlistConfigError) as raised:
                parse_numeric_allowlist(raw, field="QQ_ALLOW_FROM")
            self.assertNotIn(raw, str(raised.exception))


class AdmissionDecisionTests(unittest.TestCase):
    def _decide(
        self,
        *,
        kind: str | None,
        sender: str,
        chat_id: str | None = None,
        user_allowlist: str | None = None,
        group_allowlist: str | None = None,
    ) -> AdmissionDecision:
        env: dict[str, str] = {}
        if user_allowlist is not None:
            env["QQ_ALLOW_FROM"] = user_allowlist
        if group_allowlist is not None:
            env["QQ_ALLOW_GROUPS"] = group_allowlist
        return evaluate_admission(
            platform="qq",
            chat_kind=kind,
            chat_id=chat_id,
            sender_id=sender,
            env=env,
        )

    def test_group_only_member_is_allowed_as_group_but_denied_in_private(self) -> None:
        group = self._decide(
            kind="group",
            chat_id="30003",
            sender="40004",
            user_allowlist="20002",
            group_allowlist="30003",
        )
        private = self._decide(
            kind="p2p",
            sender="40004",
            user_allowlist="20002",
            group_allowlist="30003",
        )

        self.assertEqual(group, AdmissionDecision(True, "qq-group-allowed"))
        self.assertEqual(private, AdmissionDecision(False, "qq-private-user-not-allowed"))

    def test_every_group_is_allowed_independent_of_private_and_removed_lists(self) -> None:
        for users in (None, "", "20002", "40004", "*"):
            for groups in (None, "", "30003", "*", "invalid-old-value"):
                with self.subTest(users=users, groups=groups):
                    decision = self._decide(
                        kind="group", chat_id="30004", sender="40004",
                        user_allowlist=users, group_allowlist=groups,
                    )
                    self.assertEqual(decision, AdmissionDecision(True, "qq-group-allowed"))

    def test_missing_and_empty_private_lists_deny_private_chat(self) -> None:
        for users in (None, ""):
            with self.subTest(users=users):
                self.assertFalse(self._decide(kind="p2p", sender="40004", user_allowlist=users).allowed)

    def test_private_wildcard_allows_private_chat(self) -> None:
        self.assertTrue(self._decide(kind="p2p", sender="40004", user_allowlist="*").allowed)

    def test_invalid_group_or_sender_is_denied(self) -> None:
        for sender, group, code in (
            ("", "30003", "qq-sender-invalid"),
            ("invalid", "30003", "qq-sender-invalid"),
            ("40004", "", "qq-group-invalid"),
            ("40004", "invalid", "qq-group-invalid"),
        ):
            with self.subTest(sender=sender, group=group):
                self.assertEqual(
                    self._decide(kind="group", sender=sender, chat_id=group),
                    AdmissionDecision(False, code),
                )

    def test_malformed_config_fails_closed(self) -> None:
        with self.assertRaises(AllowlistConfigError):
            self._decide(
                kind="p2p",
                sender="40004",
                user_allowlist="40004,*",
            )

    def test_unknown_qq_chat_kind_fails_closed(self) -> None:
        decision = self._decide(
            kind="channel",
            sender="40004",
            user_allowlist="*",
        )
        self.assertEqual(decision, AdmissionDecision(False, "qq-chat-kind-invalid"))

    def test_missing_qq_chat_kind_fails_closed(self) -> None:
        for kind in (None, "", "   "):
            with self.subTest(kind=kind):
                decision = self._decide(
                    kind=kind,
                    sender="40004",
                    user_allowlist="*",
                )
                self.assertEqual(
                    decision,
                    AdmissionDecision(False, "qq-chat-kind-invalid"),
                )

    def test_non_qq_platform_is_not_subject_to_qq_lists(self) -> None:
        decision = evaluate_admission(
            platform="feishu",
            chat_kind="p2p",
            chat_id=None,
            sender_id="non-numeric-id",
            env={},
        )
        self.assertTrue(decision.allowed)

    def test_decision_is_immutable(self) -> None:
        decision = AdmissionDecision(True, "ok")
        with self.assertRaises(FrozenInstanceError):
            decision.allowed = False  # type: ignore[misc]


class RoleResolutionTests(unittest.TestCase):
    def test_qq_style_resolution_ignores_owner_display_name(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {"CHATCOPILOT_ADD_OWNER_NAMES": "Configured Owner"},
            clear=True,
        ):
            self.assertEqual(
                resolve_role(
                    user_id="40004",
                    user_name="Configured Owner",
                    allow_name_match=False,
                ),
                Role.USER,
            )

    def test_allowlists_do_not_grant_owner_or_admin_role(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {
                "CHATCOPILOT_ADD_OWNER_IDS": "10001",
                "QQ_ALLOW_FROM": "40004",
                "QQ_ALLOW_GROUPS": "30003",
            },
            clear=True,
        ):
            self.assertEqual(
                resolve_role(
                    user_id="40004",
                    user_name="Owner-like nickname",
                    allow_name_match=False,
                ),
                Role.USER,
            )


class OwnerRuntimeInfoQueryTests(unittest.TestCase):
    def _session(
        self,
        role: Role = Role.OWNER,
        *,
        chat_kind: str = "group",
        chat_id: str = "30003",
    ) -> SimpleNamespace:
        return SimpleNamespace(
            role=role,
            workspace=SimpleNamespace(chat_kind=chat_kind, chat_id=chat_id),
            runtime=SimpleNamespace(),
        )

    def test_owner_private_can_list_private_allowlist(self) -> None:
        reply = _handle_owner_runtime_info_query(
            self._session(chat_kind="p2p", chat_id=""),
            "白名单都有谁？",
            env={"QQ_ALLOW_FROM": "10002,10003", "QQ_ALLOW_GROUPS": "30003"},
        )
        self.assertIn("10002", reply or "")
        self.assertIn("10003", reply or "")
        self.assertNotIn("30003", reply or "")

    def test_group_query_reports_open_admission(self) -> None:
        reply = _handle_owner_runtime_info_query(
            self._session(),
            "此群在白名单中吗？",
            env={"QQ_ALLOW_FROM": "10002", "QQ_ALLOW_GROUPS": "30003"},
        )
        self.assertEqual(reply, "群聊无需白名单，当前群成员 @ 机器人即可交流。")
        self.assertNotIn("10002", reply or "")
        self.assertNotIn("30003", reply or "")
        self.assertNotIn("QQ_ALLOW", reply or "")

    def test_group_does_not_enumerate_or_check_other_ids(self) -> None:
        for query in ("白名单都有谁？", "10003 在白名单中吗？"):
            with self.subTest(query=query):
                reply = _handle_owner_runtime_info_query(
                    self._session(),
                    query,
                    env={
                        "QQ_ALLOW_FROM": "10002,10003",
                        "QQ_ALLOW_GROUPS": "30003",
                    },
                )
                self.assertNotIn("10002", reply or "")
                self.assertNotIn("10003", reply or "")
                self.assertNotIn("30003", reply or "")

    def test_non_owner_does_not_get_runtime_info_shortcut(self) -> None:
        self.assertIsNone(
            _handle_owner_runtime_info_query(
                self._session(Role.USER),
                "白名单都有谁？",
                env={"QQ_ALLOW_FROM": "10002", "QQ_ALLOW_GROUPS": "30003"},
            )
        )

    def test_invalid_config_does_not_echo_values(self) -> None:
        raw = "10002,*"
        reply = _handle_owner_runtime_info_query(
            self._session(chat_kind="p2p", chat_id=""),
            "白名单状态？",
            env={"QQ_ALLOW_FROM": raw, "QQ_ALLOW_GROUPS": "30003"},
        )
        self.assertIn("配置无效", reply or "")
        self.assertNotIn(raw, reply or "")


if __name__ == "__main__":
    unittest.main()
