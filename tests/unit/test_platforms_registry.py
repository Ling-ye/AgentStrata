"""platforms.registry 目录扫描自动发现 + PlatformAdapter 行为 + 部署渲染单测。"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

from chatcopilot.platforms import registry
from chatcopilot.platforms.base import PlatformAdapter
from chatcopilot.platforms.registry import UnsupportedPlatformError


class RegistryDiscoveryTests(unittest.TestCase):
    def test_discovers_feishu_and_qq(self) -> None:
        types = registry.supported_platform_types()
        self.assertIn("feishu", types)
        self.assertIn("qq", types)

    def test_get_adapter_returns_platform_adapter(self) -> None:
        adapter = registry.get_adapter("feishu")
        self.assertIsInstance(adapter, PlatformAdapter)
        self.assertEqual(adapter.name, "feishu")
        self.assertEqual(adapter.adapter_id, "feishu_acp")

    def test_is_supported_case_insensitive(self) -> None:
        self.assertTrue(registry.is_supported("FEISHU"))
        self.assertTrue(registry.is_supported(" qq "))
        self.assertFalse(registry.is_supported("wechat"))

    def test_unknown_platform_raises(self) -> None:
        with self.assertRaises(UnsupportedPlatformError):
            registry.get_adapter("wechat")


class AdapterCapabilityTests(unittest.TestCase):
    def test_feishu_capabilities_full(self) -> None:
        adapter = registry.get_adapter("feishu")
        self.assertTrue(adapter.supports_role_matrix)
        self.assertTrue(adapter.supports_user_files_pipeline)
        self.assertTrue(adapter.supports_background_jobs)

    def test_qq_capabilities_minimal(self) -> None:
        adapter = registry.get_adapter("qq")
        self.assertFalse(adapter.supports_role_matrix)
        self.assertFalse(adapter.supports_user_files_pipeline)
        self.assertTrue(adapter.supports_background_jobs)
        self.assertFalse(adapter.allow_role_name_match)

    def test_qq_resolve_user_display_name_defaults_none(self) -> None:
        self.assertIsNone(registry.get_adapter("qq").resolve_user_display_name("ou_x"))

    def test_workspace_file_delivery_preserves_legacy_adapter_signature(self) -> None:
        adapter = registry.get_adapter("feishu")
        files = [Path("report.txt")]
        with mock.patch.object(adapter, "send_files", return_value="sent") as legacy_send:
            result = adapter.send_workspace_files(
                mock.sentinel.workspace,
                files,
                message="done",
            )
        self.assertEqual(result, "sent")
        legacy_send.assert_called_once_with(files, message="done")

    def test_qq_workspace_file_delivery_receives_exact_workspace(self) -> None:
        adapter = registry.get_adapter("qq")
        files = [Path("report.txt")]
        with mock.patch(
            "chatcopilot.platforms.qq.adapter._sender.send_via_cc_connect",
            return_value="sent",
        ) as qq_send:
            result = adapter.send_workspace_files(
                mock.sentinel.workspace,
                files,
                message="done",
            )
        self.assertEqual(result, "sent")
        qq_send.assert_called_once_with(
            files,
            message="done",
            workspace=mock.sentinel.workspace,
        )


class DeployRenderTests(unittest.TestCase):
    def test_feishu_required_secrets(self) -> None:
        keys = [s.env_key for s in registry.get_adapter("feishu").required_secrets()]
        self.assertEqual(keys, ["FEISHU_APP_ID", "FEISHU_APP_SECRET"])
        self.assertTrue(all(s.required for s in registry.get_adapter("feishu").required_secrets()))

    def test_feishu_render_cc_connect_section(self) -> None:
        section = registry.get_adapter("feishu").render_cc_connect_section(
            {"FEISHU_APP_ID": "cli_x", "FEISHU_APP_SECRET": "sec_y"}
        )
        self.assertIn('type = "feishu"', section)
        self.assertIn('app_id = "cli_x"', section)
        self.assertIn('app_secret = "sec_y"', section)
        self.assertIn("resolve_mentions = true", section)
        self.assertTrue(section.endswith("\n\n"))

    def test_feishu_render_extra_files_is_lark_cli_json(self) -> None:
        files = registry.get_adapter("feishu").render_extra_files(
            {"FEISHU_APP_ID": "cli_x", "FEISHU_APP_SECRET": "sec_y"}, Path("/cc")
        )
        self.assertEqual(len(files), 1)
        path, content = next(iter(files.items()))
        self.assertTrue(path.replace("\\", "/").endswith(".lark-cli/config.json"))
        parsed = json.loads(content)
        self.assertEqual(parsed["apps"][0]["appId"], "cli_x")
        self.assertEqual(parsed["apps"][0]["appSecret"], "sec_y")

    def test_qq_required_secrets_schema(self) -> None:
        secrets = registry.get_adapter("qq").required_secrets()
        by_key = {secret.env_key: secret for secret in secrets}
        self.assertTrue(by_key["QQ_ACCOUNT"].required)
        self.assertTrue(by_key["QQ_ACCESS_TOKEN"].required)
        self.assertFalse(by_key["QQ_WS_URL"].required)
        self.assertIn("QQ_ALLOW_FROM", by_key)
        self.assertIn("QQ_ALLOW_GROUPS", by_key)
        self.assertIn("QQ_AT_PROXY_URL", by_key)
        self.assertNotIn("QQ_REQUIRE_AT_IN_GROUP", by_key)
        self.assertIn("QQ_WEBUI_PORT", by_key)
        self.assertIn("QQ_IMAGE_MAX_BYTES", by_key)
        self.assertIn("QQ_IMAGE_SEND_TIMEOUT_SECONDS", by_key)

    def test_qq_setup_actions_expose_gateway(self) -> None:
        actions = registry.get_adapter("qq").setup_actions()
        self.assertEqual([action.id for action in actions], ["qq-gateway"])
        self.assertEqual(
            actions[0].allowed_verbs,
            (
                "bootstrap",
                "sync-token",
                "start",
                "restart",
                "status",
                "logs",
            ),
        )
        self.assertIn("{verb}", actions[0].command)
        self.assertIn("{instance_id}", actions[0].command)

    def test_qq_render_cc_connect_section_defaults(self) -> None:
        token = "a" * 32
        section = registry.get_adapter("qq").render_cc_connect_section(
            {"QQ_ACCOUNT": "10001", "QQ_ACCESS_TOKEN": token}
        )
        self.assertIn('type = "qq"', section)
        self.assertIn('ws_url = "ws://127.0.0.1:3002"', section)
        self.assertIn(f'token = "{token}"', section)
        self.assertIn('allow_from = "*"', section)

    def test_qq_render_always_uses_relay_and_unrestricted_cc_connect(self) -> None:
        token = "safe_token-" + ("x" * 22)
        section = registry.get_adapter("qq").render_cc_connect_section(
            {
                "QQ_ACCOUNT": "10001",
                "QQ_WS_URL": "ws://127.0.0.1:6700",
                "QQ_AT_PROXY_URL": "ws://127.0.0.1:6701",
                "QQ_ACCESS_TOKEN": token,
                "QQ_ALLOW_FROM": "123,456",
            }
        )
        self.assertIn('ws_url = "ws://127.0.0.1:6701"', section)
        self.assertNotIn('ws_url = "ws://127.0.0.1:6700"', section)
        self.assertIn(f'token = "{token}"', section)
        self.assertIn('allow_from = "*"', section)
        self.assertNotIn('allow_from = "123,456"', section)

    def test_qq_group_allowlist_is_not_rendered_into_external_tool_config(self) -> None:
        token = "safe_token-" + ("x" * 22)
        section = registry.get_adapter("qq").render_cc_connect_section(
            {
                "QQ_ACCOUNT": "10001",
                "QQ_WS_URL": "ws://127.0.0.1:6700",
                "QQ_AT_PROXY_URL": "ws://127.0.0.1:6701",
                "QQ_ACCESS_TOKEN": token,
                "QQ_ALLOW_FROM": "20002",
                "QQ_ALLOW_GROUPS": "30003",
            }
        )
        self.assertIn('ws_url = "ws://127.0.0.1:6701"', section)
        self.assertIn('allow_from = "*"', section)
        self.assertNotIn('allow_from = "20002"', section)
        self.assertNotIn("20002", section)
        self.assertNotIn("30003", section)

    def test_qq_allowlist_validation_does_not_echo_private_value(self) -> None:
        private_value = "invalid-private-group"
        errors = registry.get_adapter("qq").validate_runtime_env(
            {
                "QQ_ACCESS_TOKEN": "x" * 32,
                "QQ_ACCOUNT": "10001",
                "QQ_ALLOW_GROUPS": private_value,
            }
        )
        self.assertTrue(any("qq_allowlist_invalid" in item for item in errors))
        self.assertNotIn(private_value, "\n".join(errors))

    def test_qq_removed_ingress_switches_are_rejected(self) -> None:
        adapter = registry.get_adapter("qq")
        base = {"QQ_ACCOUNT": "10001", "QQ_ACCESS_TOKEN": "x" * 32}
        for key in ("QQ_REQUIRE_AT_IN_GROUP", "QQ_AT_ALL_COUNTS"):
            errors = adapter.validate_runtime_env({**base, key: "false"})
            self.assertTrue(any("qq_legacy_ingress_env_removed" in item for item in errors))

    def test_qq_render_rejects_missing_weak_token_and_non_loopback_url(self) -> None:
        adapter = registry.get_adapter("qq")
        private_host = ".".join(("10", "0", "0", "1"))
        cases = (
            {},
            {"QQ_ACCOUNT": "10001", "QQ_ACCESS_TOKEN": "short"},
            {
                "QQ_ACCOUNT": "10001",
                "QQ_ACCESS_TOKEN": "x" * 32,
                "QQ_WS_URL": f"ws://{private_host}:3001",
            },
        )
        for env in cases:
            with self.subTest(env_keys=sorted(env)), self.assertRaises(ValueError):
                adapter.render_cc_connect_section(env)

    def test_qq_validation_does_not_echo_invalid_secret(self) -> None:
        secret = 'bad"token'
        errors = registry.get_adapter("qq").validate_runtime_env(
            {"QQ_ACCOUNT": "10001", "QQ_ACCESS_TOKEN": secret}
        )
        self.assertTrue(errors)
        self.assertNotIn(secret, "\n".join(errors))


if __name__ == "__main__":
    unittest.main()
