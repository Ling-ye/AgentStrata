"""Focused tests for the QQ explicit-mention Relay."""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from chatcopilot.platforms.qq.at_proxy import (
    RelayConfig,
    evaluate_forward,
    handle_downstream_connection,
    validate_relay_config,
)
from chatcopilot.platforms.qq.boundary import QQBoundaryError

BOT = "10001"


def _group(message: Any, **extra: Any) -> dict[str, Any]:
    event = {
        "post_type": "message",
        "message_type": "group",
        "group_id": 20001,
        "user_id": 10002,
        "message": message,
    }
    event.update(extra)
    return event


class ForwardDecisionTests(unittest.TestCase):
    def test_group_structured_at_bot_is_forwarded(self) -> None:
        event = _group(
            [
                {"type": "at", "data": {"qq": BOT}},
                {"type": "text", "data": {"text": " hello"}},
            ]
        )

        decision = evaluate_forward(event, BOT)

        self.assertTrue(decision.forward)
        self.assertEqual(decision.code, "group_mention_matched")
        self.assertEqual(decision.chat_kind, "group")

    def test_group_structured_at_bot_accepts_numeric_qq(self) -> None:
        event = _group([{"type": "at", "data": {"qq": 10001}}])

        self.assertTrue(evaluate_forward(event, BOT).forward)

    def test_group_without_explicit_self_at_is_dropped(self) -> None:
        cases = (
            _group([{"type": "text", "data": {"text": "hello"}}]),
            _group([{"type": "at", "data": {"qq": "99999"}}]),
            _group([{"type": "at", "data": {"qq": "all"}}]),
            _group([{"type": "text", "data": {"text": "AgentStrata hello"}}]),
        )
        for event in cases:
            with self.subTest(message=event["message"]):
                decision = evaluate_forward(event, BOT)
                self.assertFalse(decision.forward)
                self.assertEqual(decision.code, "group_mention_missing")

    def test_cq_text_is_not_treated_as_authoritative_mention(self) -> None:
        event = _group(
            "[CQ:at,qq=10001] hello",
            raw_message="[CQ:at,qq=10001] hello",
        )

        self.assertFalse(evaluate_forward(event, BOT).forward)

    def test_private_message_always_passes_relay(self) -> None:
        event = {
            "post_type": "message",
            "message_type": "private",
            "user_id": 40004,
            "message": "hello",
        }

        decision = evaluate_forward(event, BOT)

        self.assertTrue(decision.forward)
        self.assertEqual(decision.code, "private_passthrough")

    def test_non_message_frames_and_api_responses_are_transparent(self) -> None:
        cases: tuple[Any, ...] = (
            {"post_type": "meta_event", "meta_event_type": "heartbeat"},
            {"status": "ok", "retcode": 0, "echo": "1"},
            "not-an-object",
        )
        for event in cases:
            with self.subTest(event=event):
                self.assertTrue(evaluate_forward(event, BOT).forward)

    def test_unsupported_message_type_is_dropped(self) -> None:
        event = {"post_type": "message", "message_type": "guild", "message": "hello"}

        self.assertFalse(evaluate_forward(event, BOT).forward)

    def test_missing_bot_identity_fails_closed_for_group(self) -> None:
        event = _group([{"type": "at", "data": {"qq": BOT}}])

        decision = evaluate_forward(event, "")

        self.assertFalse(decision.forward)
        self.assertEqual(decision.code, "bot_identity_missing")


class _AllowlistGuard(dict[str, str]):
    def get(self, key: str, default: Any = None) -> Any:
        if key in {"QQ_ALLOW_FROM", "QQ_ALLOW_GROUPS"}:
            raise AssertionError(f"Relay read ACP-only setting: {key}")
        return super().get(key, default)


class RelayConfigTests(unittest.TestCase):
    def test_valid_config_does_not_read_acp_allowlists(self) -> None:
        config = RelayConfig(
            _AllowlistGuard(
                {
                    "QQ_ACCOUNT": BOT,
                    "QQ_ACCESS_TOKEN": "x" * 32,
                    "QQ_WS_URL": "ws://127.0.0.1:3001",
                    "QQ_AT_PROXY_URL": "ws://localhost:3002",
                }
            )
        )

        validate_relay_config(config)

        self.assertFalse(hasattr(config, "user_ids"))
        self.assertFalse(hasattr(config, "group_ids"))

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
            {
                "QQ_ACCOUNT": "not-numeric",
                "QQ_ACCESS_TOKEN": "x" * 32,
                "QQ_WS_URL": "ws://127.0.0.1:3001",
                "QQ_AT_PROXY_URL": "ws://127.0.0.1:3002",
            },
        )
        for env in cases:
            with self.subTest(env_keys=sorted(env)), self.assertRaises(QQBoundaryError):
                validate_relay_config(RelayConfig(env))


class RelayAuthenticationTests(unittest.IsolatedAsyncioTestCase):
    async def test_unauthenticated_downstream_is_closed_before_upstream_connect(self) -> None:
        connection = SimpleNamespace(
            request=SimpleNamespace(headers={}),
            close=mock.AsyncMock(),
        )
        config = RelayConfig(
            {
                "QQ_ACCOUNT": BOT,
                "QQ_ACCESS_TOKEN": "x" * 32,
                "QQ_WS_URL": "ws://127.0.0.1:3001",
                "QQ_AT_PROXY_URL": "ws://127.0.0.1:3002",
            }
        )

        with mock.patch(
            "chatcopilot.platforms.qq.at_proxy._connect_upstream",
            new=mock.AsyncMock(side_effect=AssertionError("must not connect upstream")),
        ):
            await handle_downstream_connection(connection, config)

        connection.close.assert_awaited_once_with(
            code=1008,
            reason="authentication required",
        )


class RelayStartupContractTests(unittest.TestCase):
    def test_external_processes_do_not_receive_acp_allowlists(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        start_script = (repo_root / "deploy" / "wsl" / "start.sh").read_text(encoding="utf-8")
        relay_script = (repo_root / "deploy" / "wsl" / "_start_qq_proxy.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn("env -u QQ_ALLOW_FROM -u QQ_ALLOW_GROUPS", start_script)
        self.assertIn("env -u QQ_ALLOW_FROM -u QQ_ALLOW_GROUPS", relay_script)
        self.assertNotIn("START_PROXY", relay_script)
        self.assertIn('if [ ! -f "$CC_CONFIG" ]', relay_script)
        self.assertIn("exit 3", relay_script)

    def test_relay_timeout_cleans_up_spawned_process(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        proxy_script = (repo_root / "deploy" / "wsl" / "_start_qq_proxy.sh").read_text(
            encoding="utf-8"
        )
        timeout_block = proxy_script.split('log "Relay 在 ~10s 内未就绪', maxsplit=1)[1]
        self.assertIn('kill -TERM "$NEW_PID"', timeout_block)
        self.assertIn('rm -f "$PIDFILE"', timeout_block)

    def test_relay_readiness_uses_the_validated_listener_host(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        relay_script = (repo_root / "deploy" / "wsl" / "_start_qq_proxy.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn("urlsplit(sys.argv[1])", relay_script)
        self.assertIn('"$RELAY_HOST" "$PORT"', relay_script)
        self.assertNotIn("/dev/tcp/127.0.0.1/$PORT", relay_script)
        self.assertIn('probe --url "$PROXY_URL" --url-env-key QQ_AT_PROXY_URL', relay_script)


if __name__ == "__main__":
    unittest.main()
