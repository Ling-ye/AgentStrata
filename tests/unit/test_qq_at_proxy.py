"""QQ OneBot @ 过滤代理的 should_forward 纯函数单测。"""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from chatcopilot.platforms.qq.at_proxy import (
    _ProxyConfig,
    _validate_proxy_config,
    should_forward,
)
from chatcopilot.platforms.qq.gateway_health import QQBoundaryError

BOT = "10001"


def _group(message, **extra):
    ev = {"post_type": "message", "message_type": "group", "group_id": 20001,
          "user_id": 10002, "message": message}
    ev.update(extra)
    return ev


class ShouldForwardTests(unittest.TestCase):
    # ---- 群聊：只有 @机器人 才放行 ----
    def test_group_at_bot_array_forwarded(self) -> None:
        ev = _group([{"type": "at", "data": {"qq": BOT}}, {"type": "text", "data": {"text": " 你好"}}])
        self.assertTrue(should_forward(ev, BOT))

    def test_group_at_bot_qq_as_int_forwarded(self) -> None:
        ev = _group([{"type": "at", "data": {"qq": 10001}}])
        self.assertTrue(should_forward(ev, BOT))

    def test_group_no_at_dropped(self) -> None:
        ev = _group([{"type": "text", "data": {"text": "你是谁"}}])
        self.assertFalse(should_forward(ev, BOT))

    def test_group_at_other_user_dropped(self) -> None:
        ev = _group([{"type": "at", "data": {"qq": "99999"}}, {"type": "text", "data": {"text": " hi"}}])
        self.assertFalse(should_forward(ev, BOT))

    def test_group_cq_code_raw_message_forwarded(self) -> None:
        ev = _group("[CQ:at,qq=10001] 你好", raw_message="[CQ:at,qq=10001] 你好")
        self.assertTrue(should_forward(ev, BOT))

    def test_group_cq_other_user_dropped(self) -> None:
        ev = _group("[CQ:at,qq=99999] hi", raw_message="[CQ:at,qq=99999] hi")
        self.assertFalse(should_forward(ev, BOT))

    # ---- @全体成员：默认不算，开关可放宽 ----
    def test_group_at_all_default_dropped(self) -> None:
        ev = _group([{"type": "at", "data": {"qq": "all"}}, {"type": "text", "data": {"text": " hi"}}])
        self.assertFalse(should_forward(ev, BOT))

    def test_group_at_all_counts_when_enabled(self) -> None:
        ev = _group([{"type": "at", "data": {"qq": "all"}}])
        self.assertTrue(should_forward(ev, BOT, at_all_counts=True))

    # ---- 透传：私聊 / 非 message / API 响应 ----
    def test_private_always_forwarded(self) -> None:
        ev = {"post_type": "message", "message_type": "private", "user_id": 10002,
              "message": [{"type": "text", "data": {"text": "你是谁"}}]}
        self.assertTrue(should_forward(ev, BOT))

    def test_private_requires_user_allowlist_when_proxy_enforces_access(self) -> None:
        ev = {
            "post_type": "message",
            "message_type": "private",
            "user_id": 40004,
            "message": [{"type": "text", "data": {"text": "hi"}}],
        }
        self.assertFalse(
            should_forward(
                ev,
                BOT,
                user_ids=frozenset({"20002"}),
                allow_all_users=False,
            )
        )

    def test_group_allowlist_allows_non_allowlisted_sender_with_at(self) -> None:
        ev = _group(
            [{"type": "at", "data": {"qq": BOT}}],
            group_id=30003,
            user_id=40004,
        )
        self.assertTrue(
            should_forward(
                ev,
                BOT,
                user_ids=frozenset({"20002"}),
                allow_all_users=False,
                group_ids=frozenset({"30003"}),
            )
        )

    def test_group_allowlist_does_not_grant_other_group_or_private(self) -> None:
        group = _group(
            [{"type": "at", "data": {"qq": BOT}}],
            group_id=50005,
            user_id=40004,
        )
        private = {
            "post_type": "message",
            "message_type": "private",
            "user_id": 40004,
            "message": "hi",
        }
        policy = {
            "user_ids": frozenset({"20002"}),
            "allow_all_users": False,
            "group_ids": frozenset({"30003"}),
        }
        self.assertFalse(should_forward(group, BOT, **policy))
        self.assertFalse(should_forward(private, BOT, **policy))

    def test_group_allowlist_still_honours_mention_policy(self) -> None:
        ev = _group(
            [{"type": "text", "data": {"text": "hi"}}],
            group_id=30003,
            user_id=40004,
        )
        policy = {
            "user_ids": frozenset({"20002"}),
            "allow_all_users": False,
            "group_ids": frozenset({"30003"}),
        }
        self.assertFalse(should_forward(ev, BOT, **policy))
        self.assertTrue(should_forward(ev, BOT, require_at=False, **policy))

    def test_allowlisted_user_retains_access_in_other_group(self) -> None:
        ev = _group(
            [{"type": "at", "data": {"qq": BOT}}],
            group_id=50005,
            user_id=20002,
        )
        self.assertTrue(
            should_forward(
                ev,
                BOT,
                user_ids=frozenset({"20002"}),
                allow_all_users=False,
                group_ids=frozenset({"30003"}),
            )
        )

    def test_meta_event_forwarded(self) -> None:
        self.assertTrue(should_forward({"post_type": "meta_event", "meta_event_type": "heartbeat"}, BOT))

    def test_api_response_forwarded(self) -> None:
        # API 响应没有 post_type，仅有 echo/status/data
        self.assertTrue(should_forward({"status": "ok", "retcode": 0, "echo": "1"}, BOT))

    def test_non_dict_forwarded(self) -> None:
        self.assertTrue(should_forward("not-json", BOT))

    # ---- fail-open：缺机器人号一律放行 ----
    def test_missing_bot_qq_fail_open(self) -> None:
        ev = _group([{"type": "text", "data": {"text": "你是谁"}}])
        self.assertTrue(should_forward(ev, ""))


class ProxyConfigTests(unittest.TestCase):
    def test_valid_config_is_accepted(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {
                "QQ_ACCOUNT": BOT,
                "QQ_ACCESS_TOKEN": "x" * 32,
                "QQ_WS_URL": "ws://127.0.0.1:3001",
                "QQ_AT_PROXY_URL": "ws://localhost:3002",
                "QQ_ALLOW_FROM": "20002",
                "QQ_ALLOW_GROUPS": "30003",
            },
            clear=True,
        ):
            config = _ProxyConfig()
            _validate_proxy_config(config)
            self.assertEqual(config.user_ids, frozenset({"20002"}))
            self.assertFalse(config.allow_all_users)
            self.assertEqual(config.group_ids, frozenset({"30003"}))
            self.assertFalse(config.allow_all_groups)

    def test_missing_account_weak_token_and_public_urls_are_rejected(self) -> None:
        cases = (
            {
                "QQ_ACCESS_TOKEN": "x" * 32,
                "QQ_WS_URL": "ws://127.0.0.1:3001",
                "QQ_AT_PROXY_URL": "ws://127.0.0.1:3002",
            },
            {
                "QQ_ACCOUNT": BOT,
                "QQ_ACCESS_TOKEN": "weak",
                "QQ_WS_URL": "ws://127.0.0.1:3001",
                "QQ_AT_PROXY_URL": "ws://127.0.0.1:3002",
            },
            {
                "QQ_ACCOUNT": BOT,
                "QQ_ACCESS_TOKEN": "x" * 32,
                "QQ_WS_URL": "ws://0.0.0.0:3001",
                "QQ_AT_PROXY_URL": "ws://127.0.0.1:3002",
            },
        )
        for env in cases:
            with self.subTest(env_keys=sorted(env)), mock.patch.dict(
                "os.environ",
                env,
                clear=True,
            ), self.assertRaises(QQBoundaryError):
                _validate_proxy_config(_ProxyConfig())


class ProxyStartupContractTests(unittest.TestCase):
    def test_parent_runtime_loads_qq_environment(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        start_script = (repo_root / "deploy" / "wsl" / "start.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            'ccp_load_env "FEISHU_APP_ID|FEISHU_APP_SECRET|TAVILY_API_KEY|QQ_|'
            'CHATCOPILOT_|WORKSPACE_ROOT"',
            start_script,
        )

    def test_proxy_timeout_cleans_up_spawned_process(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        proxy_script = (
            repo_root / "deploy" / "wsl" / "_start_qq_proxy.sh"
        ).read_text(encoding="utf-8")
        timeout_block = proxy_script.split(
            'log "代理在 ~10s 内未就绪', maxsplit=1
        )[1]
        self.assertIn('kill -TERM "$NEW_PID"', timeout_block)
        self.assertIn('rm -f "$PIDFILE"', timeout_block)


if __name__ == "__main__":
    unittest.main()
