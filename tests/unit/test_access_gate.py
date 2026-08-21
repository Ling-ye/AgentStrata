"""会话访问门禁（access gate）+ BotSpec access 解析 + QQ @ 检测单测。"""
from __future__ import annotations

import textwrap
import unittest
from unittest import mock
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from chatcopilot.botspec.loader import load_botspec
from chatcopilot.botspec.model import AccessSpec
from chatcopilot.middleware.acp import access_gate
from chatcopilot.middleware.acp.meta_commands import (
    _handle_owner_runtime_info_query,
    _parse_runtime_allowlist,
)
from chatcopilot.middleware.access_control import Role
from chatcopilot.middleware.access_control import resolve_role


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
    def test_access_section_parsed(self) -> None:
        block = textwrap.dedent(
            """\
            access:
              private_require_whitelist: true
              group_require_whitelist: true
              group_require_mention: true
              whitelist_env: QQ_ALLOW_FROM
              group_whitelist_env: QQ_ALLOW_GROUPS
              owner_only_project_access: true
            """
        )
        with TemporaryDirectory() as tmp:
            spec = load_botspec(_write_bot_with_access(Path(tmp), block))
        self.assertTrue(spec.access.private_require_whitelist)
        self.assertTrue(spec.access.group_require_whitelist)
        self.assertTrue(spec.access.group_require_mention)
        self.assertEqual(spec.access.whitelist_env, "QQ_ALLOW_FROM")
        self.assertEqual(spec.access.group_whitelist_env, "QQ_ALLOW_GROUPS")
        self.assertTrue(spec.access.owner_only_project_access)
        self.assertTrue(spec.access.enabled)

    def test_missing_access_defaults_disabled(self) -> None:
        with TemporaryDirectory() as tmp:
            spec = load_botspec(_write_bot_with_access(Path(tmp), ""))
        self.assertFalse(spec.access.enabled)
        self.assertIsNone(spec.access.whitelist_env)
        self.assertIsNone(spec.access.group_whitelist_env)
        self.assertFalse(spec.access.owner_only_project_access)


class RoleResolutionTests(unittest.TestCase):
    def test_qq_style_resolution_ignores_owner_display_name(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {"CHATCOPILOT_ADD_OWNER_NAMES": "Configured Owner"},
            clear=True,
        ):
            self.assertEqual(
                resolve_role(
                    user_id="not-owner",
                    user_name="Configured Owner",
                    allow_name_match=False,
                ),
                Role.USER,
            )

    def test_explicit_owner_id_still_wins_when_name_matching_is_disabled(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {"CHATCOPILOT_ADD_OWNER_IDS": "owner-001"},
            clear=True,
        ):
            self.assertEqual(
                resolve_role(
                    user_id="owner-001",
                    user_name="other",
                    allow_name_match=False,
                ),
                Role.OWNER,
            )

    def test_public_defaults_do_not_grant_owner_access(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(
                resolve_role(user_id="not-owner", user_name="Configured Owner"),
                Role.USER,
            )

    def test_qq_allowlists_do_not_grant_owner_or_admin_role(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {
                "CHATCOPILOT_ADD_OWNER_IDS": "owner-001",
                "QQ_ALLOW_FROM": "allowlisted-user",
                "QQ_ALLOW_GROUPS": "allowlisted-group",
            },
            clear=True,
        ):
            self.assertEqual(
                resolve_role(
                    user_id="allowlisted-user",
                    user_name="Owner-like nickname",
                    allow_name_match=False,
                ),
                Role.USER,
            )
            self.assertEqual(
                resolve_role(
                    user_id="member-admitted-by-group",
                    user_name="Admin-like nickname",
                    allow_name_match=False,
                ),
                Role.USER,
            )

    def test_feishu_style_name_fallback_remains_enabled(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {"CHATCOPILOT_ADD_OWNER_NAMES": "Configured Owner"},
            clear=True,
        ):
            self.assertEqual(
                resolve_role(user_id="not-owner", user_name="Configured Owner"),
                Role.OWNER,
            )


class AccessGateEvaluateTests(unittest.TestCase):
    ENABLED = AccessSpec(
        private_require_whitelist=True,
        group_require_whitelist=True,
        group_require_mention=True,
        whitelist_env="QQ_ALLOW_FROM",
        group_whitelist_env="QQ_ALLOW_GROUPS",
    )

    def _eval(
        self,
        *,
        chat_kind: str,
        user_id: str,
        text: str,
        env: dict[str, str],
        chat_id: str = "20001",
    ):
        return access_gate.evaluate(
            self.ENABLED,
            platform_type="qq",
            chat_kind=chat_kind,
            chat_id=chat_id,
            user_id=user_id,
            text=text,
            env=env,
        )

    def test_disabled_policy_passes(self) -> None:
        decision = access_gate.evaluate(
            AccessSpec(),
            platform_type="qq",
            chat_kind="group",
            user_id="x",
            text="hi",
            env={},
        )
        self.assertTrue(decision.allowed)

    def test_private_whitelisted_allowed(self) -> None:
        env = {"QQ_ALLOW_FROM": "10001,20002"}
        self.assertTrue(self._eval(chat_kind="p2p", user_id="20002", text="hi", env=env).allowed)

    def test_private_not_whitelisted_ignored(self) -> None:
        env = {"QQ_ALLOW_FROM": "10001"}
        self.assertFalse(self._eval(chat_kind="p2p", user_id="30003", text="hi", env=env).allowed)

    def test_group_whitelisted_and_mentioned_allowed(self) -> None:
        env = {"QQ_ALLOW_FROM": "20002", "QQ_ACCOUNT": "10001"}
        decision = self._eval(
            chat_kind="group", user_id="20002", text="[CQ:at,qq=10001] hi", env=env
        )
        self.assertTrue(decision.allowed)

    def test_group_no_at_marker_passes_undetermined(self) -> None:
        # cc-connect 剥掉 @ 后正文无标记 → detect 返回 None → 门禁 fail-open 放行
        # （真正的"必须@"由 cc-connect group_reply_all=false 在转发前把关）。
        env = {"QQ_ALLOW_FROM": "20002", "QQ_ACCOUNT": "10001"}
        decision = self._eval(chat_kind="group", user_id="20002", text="大家好", env=env)
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.reason, "group-mention-undetermined")

    def test_group_not_whitelisted_ignored(self) -> None:
        env = {"QQ_ALLOW_FROM": "20002", "QQ_ACCOUNT": "10001"}
        decision = self._eval(
            chat_kind="group", user_id="30003", text="[CQ:at,qq=10001] hi", env=env
        )
        self.assertFalse(decision.allowed)

    def test_group_allowlist_allows_non_allowlisted_sender(self) -> None:
        env = {
            "QQ_ALLOW_FROM": "20002",
            "QQ_ALLOW_GROUPS": "30003",
            "QQ_ACCOUNT": "10001",
        }
        decision = self._eval(
            chat_kind="group",
            chat_id="30003",
            user_id="40004",
            text="[CQ:at,qq=10001] hi",
            env=env,
        )
        self.assertTrue(decision.allowed)

    def test_group_allowlist_does_not_grant_private_access(self) -> None:
        env = {"QQ_ALLOW_FROM": "20002", "QQ_ALLOW_GROUPS": "30003"}
        decision = self._eval(
            chat_kind="p2p",
            user_id="40004",
            text="hi",
            env=env,
        )
        self.assertFalse(decision.allowed)

    def test_empty_group_allowlist_grants_no_additional_access(self) -> None:
        env = {"QQ_ALLOW_FROM": "20002", "QQ_ALLOW_GROUPS": ""}
        decision = self._eval(
            chat_kind="group",
            chat_id="30003",
            user_id="40004",
            text="hi",
            env=env,
        )
        self.assertFalse(decision.allowed)

    def test_group_wildcard_allows_any_group_but_not_private(self) -> None:
        env = {"QQ_ALLOW_FROM": "20002", "QQ_ALLOW_GROUPS": "*"}
        group = self._eval(
            chat_kind="group",
            chat_id="30003",
            user_id="40004",
            text="hi",
            env=env,
        )
        private = self._eval(
            chat_kind="p2p",
            user_id="40004",
            text="hi",
            env=env,
        )
        self.assertTrue(group.allowed)
        self.assertFalse(private.allowed)

    def test_group_mention_undetermined_passes_with_warning_reason(self) -> None:
        # 缺 QQ_ACCOUNT → 无法判定 @ → 放行（fail-open）
        env = {"QQ_ALLOW_FROM": "20002"}
        decision = self._eval(chat_kind="group", user_id="20002", text="hi", env=env)
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.reason, "group-mention-undetermined")

    def test_star_whitelist_allows_everyone(self) -> None:
        env = {"QQ_ALLOW_FROM": "*", "QQ_ACCOUNT": "10001"}
        self.assertTrue(self._eval(chat_kind="p2p", user_id="any", text="hi", env=env).allowed)


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
            runtime=SimpleNamespace(
                access=AccessSpec(
                    private_require_whitelist=True,
                    group_require_whitelist=True,
                    whitelist_env="QQ_ALLOW_FROM",
                    group_whitelist_env="QQ_ALLOW_GROUPS",
                )
            ),
        )

    def test_parse_runtime_allowlist_deduplicates_members(self) -> None:
        members, allow_all = _parse_runtime_allowlist("10001, 20002,10001")
        self.assertFalse(allow_all)
        self.assertEqual(members, ["10001", "20002"])

    def test_owner_can_query_allowlist_membership(self) -> None:
        reply = _handle_owner_runtime_info_query(
            self._session(chat_kind="p2p", chat_id=""),
            "告诉我，当前白名单有没有10003？",
            env={"QQ_ALLOW_FROM": "10002,10003", "QQ_ALLOW_GROUPS": "30003"},
        )

        self.assertIsNotNone(reply)
        self.assertIn("10003", reply or "")
        self.assertIn("在用户白名单中", reply or "")
        self.assertNotIn("10002", reply or "")
        self.assertNotIn("30003", reply or "")

    def test_owner_can_list_allowlist(self) -> None:
        reply = _handle_owner_runtime_info_query(
            self._session(chat_kind="p2p", chat_id=""),
            "白名单QQ_ALLOW_FROM都有谁？",
            env={"QQ_ALLOW_FROM": "10002,10003", "QQ_ALLOW_GROUPS": "30003"},
        )

        self.assertIsNotNone(reply)
        self.assertIn("当前共有 2 个允许来源", reply or "")
        self.assertIn("10002", reply or "")
        self.assertIn("10003", reply or "")
        self.assertIn("QQ_ALLOW_GROUPS", reply or "")

    def test_non_owner_does_not_get_runtime_info_shortcut(self) -> None:
        reply = _handle_owner_runtime_info_query(
            self._session(Role.USER),
            "白名单都有谁？",
            env={"QQ_ALLOW_FROM": "10002", "QQ_ALLOW_GROUPS": "30003"},
        )

        self.assertIsNone(reply)

    def test_star_allowlist_reports_allow_all(self) -> None:
        reply = _handle_owner_runtime_info_query(
            self._session(chat_kind="p2p", chat_id=""),
            "白名单有没有10003？",
            env={"QQ_ALLOW_FROM": "*", "QQ_ALLOW_GROUPS": "30003"},
        )

        self.assertIsNotNone(reply)
        self.assertIn("10003", reply or "")
        self.assertIn("在用户白名单中", reply or "")

    def test_owner_can_query_current_group_membership(self) -> None:
        reply = _handle_owner_runtime_info_query(
            self._session(),
            "告诉我，此群在白名单中吗？",
            env={"QQ_ALLOW_FROM": "10002", "QQ_ALLOW_GROUPS": "30003"},
        )

        self.assertIsNotNone(reply)
        self.assertEqual(reply, "当前群在群聊白名单中。")
        self.assertNotIn("10002", reply or "")
        self.assertNotIn("30003", reply or "")
        self.assertNotIn("QQ_ALLOW_FROM", reply or "")
        self.assertNotIn("QQ_ALLOW_GROUPS", reply or "")

    def test_empty_group_allowlist_reports_current_group_denied(self) -> None:
        reply = _handle_owner_runtime_info_query(
            self._session(),
            "此群在群白名单中吗？",
            env={"QQ_ALLOW_FROM": "10002", "QQ_ALLOW_GROUPS": ""},
        )

        self.assertIsNotNone(reply)
        self.assertEqual(reply, "当前群不在群聊白名单中。")
        self.assertNotIn("10002", reply or "")
        self.assertNotIn("30003", reply or "")

    def test_group_chat_refuses_full_allowlist_enumeration(self) -> None:
        reply = _handle_owner_runtime_info_query(
            self._session(),
            "白名单都有谁？",
            env={"QQ_ALLOW_FROM": "10002,10003", "QQ_ALLOW_GROUPS": "30003"},
        )

        self.assertIsNotNone(reply)
        self.assertIn("不能在这里列出完整名单", reply or "")
        self.assertNotIn("10002", reply or "")
        self.assertNotIn("10003", reply or "")
        self.assertNotIn("30003", reply or "")

    def test_group_chat_refuses_explicit_id_membership_query(self) -> None:
        reply = _handle_owner_runtime_info_query(
            self._session(),
            "10003 在白名单中吗？",
            env={"QQ_ALLOW_FROM": "10002,10003", "QQ_ALLOW_GROUPS": "30003"},
        )

        self.assertIsNotNone(reply)
        self.assertIn("其他查询请由 Owner 私聊", reply or "")
        self.assertNotIn("10002", reply or "")
        self.assertNotIn("10003", reply or "")
        self.assertNotIn("30003", reply or "")

    def test_private_generic_query_reports_counts_without_identities(self) -> None:
        reply = _handle_owner_runtime_info_query(
            self._session(chat_kind="p2p", chat_id=""),
            "当前白名单是什么状态？",
            env={"QQ_ALLOW_FROM": "10002,10003", "QQ_ALLOW_GROUPS": "30003"},
        )

        self.assertIsNotNone(reply)
        self.assertIn("2 个用户白名单条目", reply or "")
        self.assertIn("1 个群聊白名单条目", reply or "")
        self.assertNotIn("10002", reply or "")
        self.assertNotIn("10003", reply or "")
        self.assertNotIn("30003", reply or "")


if __name__ == "__main__":
    unittest.main()
