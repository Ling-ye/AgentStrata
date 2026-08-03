from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from chatcopilot.platforms.qq.gateway_health import (
    QQBoundaryError,
    _connect_once,
    probe_onebot_boundary,
    require_access_token,
    require_loopback_websocket_url,
)


class QQBoundaryValidationTests(unittest.TestCase):
    def test_token_requires_32_to_128_url_safe_characters(self) -> None:
        self.assertEqual(require_access_token("a" * 32), "a" * 32)
        for token in ("", "a" * 31, "a" * 129, 'a"b' + ("x" * 30)):
            with self.subTest(length=len(token)), self.assertRaises(QQBoundaryError):
                require_access_token(token)

    def test_websocket_url_requires_explicit_loopback_port(self) -> None:
        accepted = (
            "ws://127.0.0.1:3001",
            "ws://localhost:3002/path",
            "wss://[::1]:3001",
        )
        for url in accepted:
            with self.subTest(url=url):
                self.assertEqual(
                    require_loopback_websocket_url(url, env_key="QQ_WS_URL"),
                    url,
                )
        private_host = ".".join(("10", "0", "0", "1"))
        rejected = (
            f"ws://{private_host}:3001",
            "ws://localhost" + ".evil:3001",
            "http://127.0.0.1:3001",
            "ws://127.0.0.1",
        )
        for url in rejected:
            with self.subTest(url=url), self.assertRaises(QQBoundaryError):
                require_loopback_websocket_url(url, env_key="QQ_WS_URL")

    def test_gateway_script_binds_ports_to_loopback_and_preserves_volumes(self) -> None:
        script = (
            Path(__file__).resolve().parents[2] / "deploy" / "wsl" / "qq_gateway.sh"
        ).read_text(encoding="utf-8")
        self.assertIn('-p "127.0.0.1:$WS_PORT:3001"', script)
        self.assertIn('-p "127.0.0.1:$QQ_WEBUI_PORT:6099"', script)
        self.assertNotIn('-p "$WS_PORT:3001"', script)
        self.assertNotIn('-p "$QQ_WEBUI_PORT:6099"', script)
        self.assertIn("container_ports_are_loopback", script)
        self.assertIn("bootstrap)", script)
        self.assertIn("restart)", script)
        self.assertIn("sync-token)", script)
        self.assertIn('docker restart "$CONTAINER"', script)
        self.assertIn('CHATCOPILOT_HOME="$REPO_ROOT"', script)
        self.assertIn('--bot "$REPO_ROOT/bots/$INSTANCE/bot.yaml"', script)
        self.assertIn('--config "$LOCAL_CONFIG"', script)
        self.assertIn("--entrypoint python3", script)
        self.assertIn(
            'elif [ "$ACTION" = "start" ] || [ "$ACTION" = "restart" ]',
            script,
        )
        self.assertIn("validate-url", script)
        self.assertIn('probe_output="$(probe_boundary 2>&1)"', script)
        self.assertIn("printf '%s\\n' \"$probe_output\"", script)
        self.assertIn('container_volume_name "/app/.config/QQ"', script)
        self.assertIn('container_volume_name "/app/napcat/config"', script)

    def test_service_start_fails_closed_when_mention_proxy_fails(self) -> None:
        script = (
            Path(__file__).resolve().parents[2] / "deploy" / "wsl" / "start.sh"
        ).read_text(encoding="utf-8")
        self.assertIn("拒绝启动 cc-connect", script)
        self.assertNotIn("降级：cc-connect 直连 NapCat", script)
        self.assertNotIn('sed -i "s#^ws_url =', script)


class QQBoundaryProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_connection_probe_executes_onebot_action(self) -> None:
        connection = SimpleNamespace(
            send=mock.AsyncMock(),
            recv=mock.AsyncMock(
                side_effect=(
                    json.dumps(
                        {
                            "post_type": "meta_event",
                            "meta_event_type": "lifecycle",
                        }
                    ),
                    json.dumps(
                        {
                            "status": "ok",
                            "retcode": 0,
                            "echo": "chatcopilot-onebot-auth-probe",
                        }
                    ),
                )
            ),
            close=mock.AsyncMock(),
        )
        websockets = SimpleNamespace(connect=mock.AsyncMock(return_value=connection))
        with mock.patch.dict(sys.modules, {"websockets": websockets}):
            await _connect_once("ws://127.0.0.1:3001", "a" * 32)

        request = json.loads(connection.send.await_args.args[0])
        self.assertEqual(request["action"], "get_status")
        self.assertEqual(request["echo"], "chatcopilot-onebot-auth-probe")
        connection.close.assert_awaited_once()

    async def test_connection_probe_treats_post_handshake_1403_as_rejection(
        self,
    ) -> None:
        connection = SimpleNamespace(
            send=mock.AsyncMock(),
            recv=mock.AsyncMock(
                return_value=json.dumps(
                    {
                        "status": "failed",
                        "retcode": 1403,
                        "message": "token verify failed",
                    }
                )
            ),
            close=mock.AsyncMock(),
        )
        websockets = SimpleNamespace(connect=mock.AsyncMock(return_value=connection))
        with (
            mock.patch.dict(sys.modules, {"websockets": websockets}),
            self.assertRaises(PermissionError),
        ):
            await _connect_once("ws://127.0.0.1:3001", None)

        connection.close.assert_awaited_once()

    async def test_probe_requires_rejection_then_authenticated_success(self) -> None:
        with mock.patch(
            "chatcopilot.platforms.qq.gateway_health._connect_once",
            new=mock.AsyncMock(side_effect=(RuntimeError("unauthorized"), None)),
        ) as connect:
            await probe_onebot_boundary("ws://127.0.0.1:3001", "a" * 32)

        self.assertEqual(connect.await_count, 2)
        self.assertEqual(connect.await_args_list[0].args[1], None)
        self.assertEqual(connect.await_args_list[1].args[1], "a" * 32)

    async def test_probe_rejects_server_that_accepts_no_token(self) -> None:
        with mock.patch(
            "chatcopilot.platforms.qq.gateway_health._connect_once",
            new=mock.AsyncMock(return_value=None),
        ), self.assertRaises(QQBoundaryError) as caught:
            await probe_onebot_boundary("ws://127.0.0.1:3001", "a" * 32)

        self.assertEqual(
            caught.exception.error_code,
            "qq_onebot_accepts_unauthenticated",
        )

    async def test_probe_normalizes_authenticated_failure(self) -> None:
        with mock.patch(
            "chatcopilot.platforms.qq.gateway_health._connect_once",
            new=mock.AsyncMock(
                side_effect=(RuntimeError("unauthorized"), RuntimeError("bad token"))
            ),
        ), self.assertRaises(QQBoundaryError) as caught:
            await probe_onebot_boundary("ws://127.0.0.1:3001", "a" * 32)

        self.assertEqual(
            caught.exception.error_code,
            "qq_onebot_authenticated_probe_failed",
        )


if __name__ == "__main__":
    unittest.main()
