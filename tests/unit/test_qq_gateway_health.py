from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from chatcopilot.platforms.qq.gateway_health import (
    OneBotRuntimeStatus,
    QQBoundaryError,
    _connect_once,
    probe_onebot_boundary,
    probe_onebot_online,
    require_access_token,
    require_loopback_websocket_url,
    run_qq_external_checks,
)


TOKEN = "test_" + ("a" * 32)
BOT_ACCOUNT = "1" + "0001"
GROUP_ID = "2" + "0002"
ONLINE_STATUS = OneBotRuntimeStatus(online=True, good=True)


def _gateway_test_root(tmpdir: str) -> Path:
    """Create the minimal file tree needed to reach gateway preflight checks."""
    source_root = Path(__file__).resolve().parents[2]
    root = Path(tmpdir) / "repo"
    scripts = root / "deploy" / "wsl"
    scripts.mkdir(parents=True)
    for name in ("qq_gateway.sh", "_load_env.sh"):
        shutil.copy2(source_root / "deploy" / "wsl" / name, scripts / name)

    bot_dir = root / "bots" / "test-assistant"
    bot_dir.mkdir(parents=True)
    (bot_dir / "bot.yaml").write_text(
        "id: test-assistant\n"
        "gateway:\n"
        "  protocol_version: 1\n"
        "channels:\n"
        "  qq:\n"
        "    type: qq_personal\n",
        encoding="utf-8",
    )
    local_env = bot_dir / "local.env"
    local_env.write_text("QQ_ACCOUNT=10001\n", encoding="utf-8")
    local_env.chmod(0o600)
    return root


def _fake_docker(bin_dir: Path, calls: Path) -> None:
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$FAKE_DOCKER_CALLS\"\n"
        "case \"${1:-}\" in\n"
        "  ps) printf '%s\\n' napcat-test-assistant ;;\n"
        "  inspect)\n"
        "    case \"$*\" in\n"
        "      *.Config.Image*) printf '%s\\n' \"$NAPCAT_EXPECTED_IMAGE\" ;;\n"
        "      *.Config.Env*) printf '%s\\n' \"NAPCAT_DISABLE_BYPASS=1\" \"NAPCAT_DISABLE_MULTI_PROCESS=1\" \"ACCOUNT=10001\" \"NODE_ENV=production\" ;;\n"
        "      *.HostConfig.ShmSize*) printf '%s\\n' \"${FAKE_DOCKER_SHM_BYTES:-536870912}\" ;;\n"
        "    esac ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)


def _external_env(*, group: bool = True) -> dict[str, str]:
    result = {
        "CHATCOPILOT_QQ_ONEBOT_WS_URL": "ws://127.0.0.1:3001",
        "QQ_ACCESS_TOKEN": TOKEN,
        "QQ_ACCOUNT": BOT_ACCOUNT,
    }
    if group:
        result["CHATCOPILOT_EXTERNAL_CHECK_QQ_GROUP_ID"] = GROUP_ID
    return result


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
        self.assertIn("-m chatcopilot.botspec.qq_token_sync", script)
        self.assertNotIn("sync-local-env", script)
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
        self.assertIn("run_external_check()", script)
        self.assertIn("-m chatcopilot bot external-check", script)
        self.assertIn('if ! run_external_check; then', script)
        self.assertIn('container_volume_name "/app/.config/QQ"', script)
        self.assertIn('container_volume_name "/app/napcat/config"', script)
        self.assertIn(
            'DEFAULT_NAPCAT_IMAGE="mlikiowa/napcat-docker@sha256:'
            '0b4b24114089bfbbefd4729ad08b50a6b9d67044aec674809ede3cf7521c4431"',
            script,
        )
        self.assertIn("container_image_matches", script)
        self.assertGreaterEqual(script.count('"$NAPCAT_IMAGE"'), 3)
        self.assertNotIn("mlikiowa/napcat-docker:latest", script)
        self.assertIn('if ! docker run -d --name "$CONTAINER"', script)
        self.assertIn('servers = network.setdefault("websocketServers", [])', script)
        self.assertIn('"name": "agentstrata-websocket-server"', script)
        self.assertIn('server["host"] = "0.0.0.0"', script)
        self.assertIn('if ! docker start "$CONTAINER" >/dev/null; then', script)
        self.assertIn("if ! recreate_container; then", script)
        self.assertIn('err "创建 NapCat 容器失败：$CONTAINER"', script)

    def test_gateway_script_rejects_mutable_image_override_before_docker(self) -> None:
        root = Path(__file__).resolve().parents[2]
        completed = subprocess.run(
            [
                "bash",
                str(root / "deploy" / "wsl" / "qq_gateway.sh"),
                "logs",
                "--instance",
                "lingye-copilot-qq",
            ],
            cwd=root,
            env={**os.environ, "NAPCAT_IMAGE": "mlikiowa/napcat-docker:latest"},
            capture_output=True,
            text=True,
            timeout=10,
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("name@sha256:<64hex>", completed.stderr)

    def test_provider_manager_rejects_legacy_qq_topology(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = _gateway_test_root(tmpdir)
            bot_spec = root / "bots" / "test-assistant" / "bot.yaml"
            bot_spec.write_text(
                "id: test-assistant\nplatform:\n  type: qq\n",
                encoding="utf-8",
            )
            completed = subprocess.run(
                [
                    "bash",
                    str(root / "deploy" / "wsl" / "qq_gateway.sh"),
                    "logs",
                    "--instance",
                    "test-assistant",
                ],
                cwd=root,
                env={**os.environ, "HOME": tmpdir},
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )

        self.assertEqual(completed.returncode, 78)
        self.assertIn("旧 platform=qq + cc-connect/Relay 拓扑已移除", completed.stderr)

    def test_gateway_rejects_malformed_account_before_any_docker_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = _gateway_test_root(tmpdir)
            bin_dir = Path(tmpdir) / "bin"
            bin_dir.mkdir()
            calls = Path(tmpdir) / "docker.calls"
            _fake_docker(bin_dir, calls)

            completed = subprocess.run(
                [
                    "bash",
                    str(root / "deploy" / "wsl" / "qq_gateway.sh"),
                    "bootstrap",
                    "--instance",
                    "test-assistant",
                ],
                cwd=root,
                env={
                    **os.environ,
                    "HOME": tmpdir,
                    "CHATCOPILOT_ENV_FILE": str(Path(tmpdir) / "missing.env"),
                    "QQ_ACCOUNT": "10001; docker run unexpected",
                    "PATH": f"{bin_dir}:{os.environ['PATH']}",
                    "FAKE_DOCKER_CALLS": str(calls),
                },
                capture_output=True,
                text=True,
                timeout=10,
            )
            docker_calls = calls.read_text(encoding="utf-8") if calls.exists() else ""

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("QQ_ACCOUNT 必须是纯数字", completed.stderr)
        self.assertEqual(docker_calls, "")

    def test_gateway_rejects_invalid_shm_override_before_any_docker_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = _gateway_test_root(tmpdir)
            bin_dir = Path(tmpdir) / "bin"
            bin_dir.mkdir()
            calls = Path(tmpdir) / "docker.calls"
            _fake_docker(bin_dir, calls)

            completed = subprocess.run(
                [
                    "bash",
                    str(root / "deploy" / "wsl" / "qq_gateway.sh"),
                    "bootstrap",
                    "--instance",
                    "test-assistant",
                ],
                cwd=root,
                env={
                    **os.environ,
                    "HOME": tmpdir,
                    "CHATCOPILOT_ENV_FILE": str(Path(tmpdir) / "missing.env"),
                    "QQ_ACCOUNT": "10001",
                    "NAPCAT_SHM_SIZE": "512mb; docker run unexpected",
                    "PATH": f"{bin_dir}:{os.environ['PATH']}",
                    "FAKE_DOCKER_CALLS": str(calls),
                },
                capture_output=True,
                text=True,
                timeout=10,
            )
            docker_calls = calls.read_text(encoding="utf-8") if calls.exists() else ""

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("NAPCAT_SHM_SIZE 必须是正整数", completed.stderr)
        self.assertEqual(docker_calls, "")

    def test_gateway_recreates_when_container_shm_does_not_exactly_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = _gateway_test_root(tmpdir)
            bin_dir = Path(tmpdir) / "bin"
            bin_dir.mkdir()
            calls = Path(tmpdir) / "docker.calls"
            _fake_docker(bin_dir, calls)
            python = root / ".venv" / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.write_text(
                "#!/usr/bin/env bash\n"
                "if [ \"${1:-}\" = \"-\" ]; then\n"
                "  payload=$(cat)\n"
                "  case \"$payload\" in *urlparse*) printf '3001\\n' ;; esac\n"
                "fi\n",
                encoding="utf-8",
            )
            python.chmod(0o755)
            expected_image = (
                "mlikiowa/napcat-docker@sha256:"
                "0b4b24114089bfbbefd4729ad08b50a6b9d67044aec674809ede3cf7521c4431"
            )

            completed = subprocess.run(
                [
                    "bash",
                    str(root / "deploy" / "wsl" / "qq_gateway.sh"),
                    "start",
                    "--instance",
                    "test-assistant",
                ],
                cwd=root,
                env={
                    **os.environ,
                    "HOME": tmpdir,
                    "CHATCOPILOT_ENV_FILE": str(Path(tmpdir) / "missing.env"),
                    "QQ_ACCOUNT": "10001",
                    "QQ_ACCESS_TOKEN": "a" * 32,
                    "PATH": f"{bin_dir}:{os.environ['PATH']}",
                    "FAKE_DOCKER_CALLS": str(calls),
                    "FAKE_DOCKER_SHM_BYTES": "268435456",
                    "NAPCAT_EXPECTED_IMAGE": expected_image,
                },
                capture_output=True,
                text=True,
                timeout=10,
            )

            docker_calls = calls.read_text(encoding="utf-8")

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("重建容器需要可信交互式终端确认", completed.stderr)
        self.assertIn("inspect napcat-test-assistant --format {{.HostConfig.ShmSize}}", docker_calls)
        self.assertNotIn(" start napcat-test-assistant", f" {docker_calls}")
        self.assertNotIn(" stop napcat-test-assistant", f" {docker_calls}")
        self.assertNotIn(" rm napcat-test-assistant", f" {docker_calls}")

    def test_service_start_fails_closed_when_legacy_cleanup_fails(self) -> None:
        script = (
            Path(__file__).resolve().parents[2] / "deploy" / "wsl" / "start.sh"
        ).read_text(encoding="utf-8")
        self.assertIn("拒绝启动 Gateway，避免双重投递", script)
        self.assertIn('exec "$PY" -m chatcopilot run --bot "$BOT_SPEC"', script)
        self.assertNotIn("_start_qq_proxy.sh", script)
        self.assertNotIn("降级：cc-connect 直连 NapCat", script)


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
                            "data": {"online": True, "good": True},
                        }
                    ),
                )
            ),
            close=mock.AsyncMock(),
        )
        websockets = SimpleNamespace(connect=mock.AsyncMock(return_value=connection))
        with mock.patch.dict(sys.modules, {"websockets": websockets}):
            status = await _connect_once("ws://127.0.0.1:3001", "a" * 32)

        self.assertEqual(status, ONLINE_STATUS)
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
            new=mock.AsyncMock(side_effect=(RuntimeError("unauthorized"), ONLINE_STATUS)),
        ) as connect:
            status = await probe_onebot_boundary("ws://127.0.0.1:3001", "a" * 32)

        self.assertEqual(status, ONLINE_STATUS)
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

    async def test_online_probe_rejects_reachable_offline_account(self) -> None:
        with mock.patch(
            "chatcopilot.platforms.qq.gateway_health.probe_onebot_boundary",
            new=mock.AsyncMock(
                return_value=OneBotRuntimeStatus(online=False, good=True)
            ),
        ), self.assertRaises(QQBoundaryError) as caught:
            await probe_onebot_online("ws://127.0.0.1:3001", "a" * 32)

        self.assertEqual(caught.exception.error_code, "qq_account_offline")

    async def test_online_probe_rejects_unhealthy_provider(self) -> None:
        with mock.patch(
            "chatcopilot.platforms.qq.gateway_health.probe_onebot_boundary",
            new=mock.AsyncMock(
                return_value=OneBotRuntimeStatus(online=True, good=False)
            ),
        ), self.assertRaises(QQBoundaryError) as caught:
            await probe_onebot_online("ws://127.0.0.1:3001", "a" * 32)

        self.assertEqual(caught.exception.error_code, "qq_onebot_unhealthy")


class QQExternalPlatformCheckTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_only_check_is_outside_agent_evaluation_and_secret_free(self) -> None:
        async def action(
            _url: str,
            _token: str,
            *,
            action: str,
            params: dict[str, object],
            echo: str,
        ) -> dict[str, object]:
            del params, echo
            if action == "get_login_info":
                return {
                    "status": "ok",
                    "retcode": 0,
                    "data": {"user_id": int(BOT_ACCOUNT), "nickname": "private-name"},
                }
            if action == "get_group_info":
                return {
                    "status": "ok",
                    "retcode": 0,
                    "data": {"group_id": int(GROUP_ID), "group_name": "private-group"},
                }
            raise AssertionError(action)

        with (
            mock.patch(
                "chatcopilot.platforms.qq.gateway_health.probe_onebot_boundary",
                new=mock.AsyncMock(return_value=ONLINE_STATUS),
            ),
            mock.patch(
                "chatcopilot.platforms.qq.gateway_health._onebot_action",
                new=mock.AsyncMock(side_effect=action),
            ) as onebot_action,
        ):
            report = await run_qq_external_checks(
                _external_env(),
                bot_id="example-qq-bot",
            )

        self.assertEqual(report.verdict, "passed")
        self.assertEqual(report.scope, "external_platform")
        self.assertFalse(report.agent_evaluation)
        self.assertFalse(report.external_write_attempted)
        self.assertFalse(report.external_write_performed)
        self.assertEqual(onebot_action.await_count, 2)
        checks = {item.check_id: item for item in report.checks}
        self.assertEqual(checks["qq_account_online"].status, "passed")
        self.assertEqual(checks["qq_simulated_gateway_ingress"].status, "passed")
        self.assertEqual(
            checks["qq_simulated_gateway_ingress"].evidence["mode"],
            "hermetic_loopback",
        )
        self.assertEqual(checks["qq_inbound_agent_roundtrip"].status, "not_tested")
        serialized = json.dumps(report.to_dict(), ensure_ascii=False, sort_keys=True)
        for private_value in (
            TOKEN,
            BOT_ACCOUNT,
            GROUP_ID,
            "private-name",
            "private-group",
        ):
            self.assertNotIn(private_value, serialized)

    async def test_group_check_is_optional_for_read_only_probe(self) -> None:
        with (
            mock.patch(
                "chatcopilot.platforms.qq.gateway_health.probe_onebot_boundary",
                new=mock.AsyncMock(return_value=ONLINE_STATUS),
            ),
            mock.patch(
                "chatcopilot.platforms.qq.gateway_health._onebot_action",
                new=mock.AsyncMock(
                    return_value={
                        "status": "ok",
                        "retcode": 0,
                        "data": {"user_id": int(BOT_ACCOUNT)},
                    }
                ),
            ) as onebot_action,
        ):
            report = await run_qq_external_checks(
                _external_env(group=False),
                bot_id="example-qq-bot",
            )

        self.assertEqual(report.verdict, "passed")
        self.assertEqual(onebot_action.await_count, 1)
        group_check = next(item for item in report.checks if item.check_id == "qq_group_access")
        self.assertEqual(group_check.status, "not_configured")
        self.assertFalse(group_check.required)

    async def test_external_write_requires_two_explicit_flags_before_network(self) -> None:
        with (
            mock.patch(
                "chatcopilot.platforms.qq.gateway_health.probe_onebot_boundary",
                new=mock.AsyncMock(),
            ) as boundary,
            mock.patch(
                "chatcopilot.platforms.qq.gateway_health._onebot_action",
                new=mock.AsyncMock(),
            ) as onebot_action,
        ):
            report = await run_qq_external_checks(
                _external_env(),
                bot_id="example-qq-bot",
                send_message=True,
                confirm_external_write=False,
            )

        self.assertEqual(report.verdict, "failed")
        self.assertFalse(report.external_write_attempted)
        boundary.assert_not_awaited()
        onebot_action.assert_not_awaited()

    async def test_confirmed_outbound_probe_uses_fixed_group_and_hides_receipt(self) -> None:
        actions: list[tuple[str, dict[str, object]]] = []

        async def action(
            _url: str,
            _token: str,
            *,
            action: str,
            params: dict[str, object],
            echo: str,
        ) -> dict[str, object]:
            del echo
            actions.append((action, params))
            if action == "get_login_info":
                data: dict[str, object] = {"user_id": int(BOT_ACCOUNT)}
            elif action == "get_group_info":
                data = {"group_id": int(GROUP_ID)}
            elif action == "send_group_msg":
                data = {"message_id": "raw-private-message-id"}
            else:
                raise AssertionError(action)
            return {"status": "ok", "retcode": 0, "data": data}

        with (
            mock.patch(
                "chatcopilot.platforms.qq.gateway_health.probe_onebot_boundary",
                new=mock.AsyncMock(return_value=ONLINE_STATUS),
            ),
            mock.patch(
                "chatcopilot.platforms.qq.gateway_health._onebot_action",
                new=mock.AsyncMock(side_effect=action),
            ),
        ):
            report = await run_qq_external_checks(
                _external_env(),
                bot_id="example-qq-bot",
                send_message=True,
                confirm_external_write=True,
            )

        self.assertEqual(report.verdict, "passed")
        self.assertTrue(report.external_write_attempted)
        self.assertTrue(report.external_write_performed)
        self.assertEqual([item[0] for item in actions], [
            "get_login_info",
            "get_group_info",
            "send_group_msg",
        ])
        send_params = actions[-1][1]
        self.assertEqual(send_params["group_id"], int(GROUP_ID))
        self.assertIn("[AgentStrata external check] nonce=", str(send_params["message"]))
        serialized = json.dumps(report.to_dict(), ensure_ascii=False, sort_keys=True)
        self.assertNotIn("raw-private-message-id", serialized)
        self.assertNotIn(GROUP_ID, serialized)

    async def test_authenticated_boundary_failure_stops_followup_actions(self) -> None:
        with (
            mock.patch(
                "chatcopilot.platforms.qq.gateway_health.probe_onebot_boundary",
                new=mock.AsyncMock(
                    side_effect=QQBoundaryError(
                        "qq_onebot_authenticated_probe_failed",
                        "probe failed",
                    )
                ),
            ),
            mock.patch(
                "chatcopilot.platforms.qq.gateway_health._onebot_action",
                new=mock.AsyncMock(),
            ) as onebot_action,
        ):
            report = await run_qq_external_checks(
                _external_env(),
                bot_id="example-qq-bot",
            )

        self.assertEqual(report.verdict, "error")
        onebot_action.assert_not_awaited()
        checks = {item.check_id: item for item in report.checks}
        self.assertEqual(checks["qq_account_online"].status, "not_tested")
        self.assertEqual(checks["qq_login_identity"].status, "not_tested")
        self.assertEqual(checks["qq_group_access"].status, "not_tested")
        self.assertEqual(checks["qq_simulated_gateway_ingress"].status, "passed")

    async def test_simulated_ingress_failure_is_an_external_check_error(self) -> None:
        async def action(
            _url: str,
            _token: str,
            *,
            action: str,
            params: dict[str, object],
            echo: str,
        ) -> dict[str, object]:
            del params, echo
            if action == "get_login_info":
                data: dict[str, object] = {"user_id": int(BOT_ACCOUNT)}
            elif action == "get_group_info":
                data = {"group_id": int(GROUP_ID)}
            else:
                raise AssertionError(action)
            return {"status": "ok", "retcode": 0, "data": data}

        with (
            mock.patch(
                "chatcopilot.platforms.qq.gateway_health.probe_onebot_boundary",
                new=mock.AsyncMock(return_value=ONLINE_STATUS),
            ),
            mock.patch(
                "chatcopilot.platforms.qq.gateway_health._onebot_action",
                new=mock.AsyncMock(side_effect=action),
            ),
            mock.patch(
                "chatcopilot.platforms.qq.ingress_probe.run_simulated_gateway_ingress",
                new=mock.AsyncMock(side_effect=RuntimeError("fixture failure")),
            ),
        ):
            report = await run_qq_external_checks(
                _external_env(),
                bot_id="example-qq-bot",
            )

        self.assertEqual(report.verdict, "error")
        checks = {item.check_id: item for item in report.checks}
        self.assertEqual(checks["qq_simulated_gateway_ingress"].status, "error")
        self.assertNotIn("fixture failure", checks["qq_simulated_gateway_ingress"].detail)

    async def test_offline_account_fails_before_identity_or_group_actions(self) -> None:
        with (
            mock.patch(
                "chatcopilot.platforms.qq.gateway_health.probe_onebot_boundary",
                new=mock.AsyncMock(
                    return_value=OneBotRuntimeStatus(online=False, good=True)
                ),
            ),
            mock.patch(
                "chatcopilot.platforms.qq.gateway_health._onebot_action",
                new=mock.AsyncMock(),
            ) as onebot_action,
        ):
            report = await run_qq_external_checks(
                _external_env(),
                bot_id="example-qq-bot",
            )

        self.assertEqual(report.verdict, "failed")
        onebot_action.assert_not_awaited()
        checks = {item.check_id: item for item in report.checks}
        self.assertEqual(checks["onebot_boundary"].status, "passed")
        self.assertEqual(checks["qq_account_online"].status, "failed")
        self.assertEqual(checks["qq_account_online"].detail, "QQ 账号离线")
        self.assertEqual(checks["qq_login_identity"].status, "not_tested")
        self.assertEqual(checks["qq_group_access"].status, "not_tested")


if __name__ == "__main__":
    unittest.main()
