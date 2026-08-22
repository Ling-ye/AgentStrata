from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from chatcopilot.core.ingress_receipts import consume_ingress_receipt
from chatcopilot.platforms.qq.at_proxy import _ProxyConfig
from chatcopilot.platforms.qq.access_proxy import normalized_onebot_text
from chatcopilot.platforms.qq.ingress_probe import run_simulated_gateway_ingress


_TOKEN = "probe_" + ("x" * 32)
_BOT = "1" + "0001"
_USER = "2" + "0002"
_GROUP = "3" + "0003"


def _env(*, require_at: bool = True) -> dict[str, str]:
    return {
        "QQ_ACCESS_TOKEN": _TOKEN,
        "QQ_ACCOUNT": _BOT,
        "QQ_ALLOW_FROM": _USER,
        "QQ_ALLOW_GROUPS": _GROUP,
        "QQ_REQUIRE_AT_IN_GROUP": "true" if require_at else "false",
        "CHATCOPILOT_EXTERNAL_CHECK_QQ_GROUP_ID": _GROUP,
    }


class SimulatedGatewayIngressTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_relay_forwards_positive_and_drops_missing_at(self) -> None:
        receipt = await run_simulated_gateway_ingress(_env())

        self.assertTrue(receipt.passed)
        self.assertTrue(receipt.upstream_authenticated)
        self.assertTrue(receipt.positive_forwarded)
        self.assertTrue(receipt.negative_dropped)
        self.assertEqual(receipt.mode, "hermetic_loopback")
        self.assertEqual(len(receipt.positive_frame_sha256), 64)
        self.assertEqual(len(receipt.negative_frame_sha256), 64)

    async def test_policy_without_at_uses_an_unsupported_message_negative(self) -> None:
        receipt = await run_simulated_gateway_ingress(_env(require_at=False))

        self.assertTrue(receipt.passed)
        self.assertTrue(receipt.negative_dropped)

    async def test_evidence_contains_no_identity_token_or_message_text(self) -> None:
        receipt = await run_simulated_gateway_ingress(_env())

        serialized = json.dumps(receipt.to_evidence(), sort_keys=True)
        for private_value in (_TOKEN, _BOT, _USER, _GROUP, "ingress-probe"):
            self.assertNotIn(private_value, serialized)

    async def test_relay_logs_never_receive_private_probe_configuration(self) -> None:
        with self.assertLogs(
            "chatcopilot.platforms.qq.at_proxy",
            level="INFO",
        ) as captured:
            receipt = await run_simulated_gateway_ingress(_env())

        self.assertTrue(receipt.passed)
        serialized = "\n".join(captured.output)
        for private_value in (_TOKEN, _BOT, _USER, _GROUP):
            self.assertNotIn(private_value, serialized)

    async def test_ephemeral_listeners_are_released_between_runs(self) -> None:
        first = await run_simulated_gateway_ingress(_env())
        second = await run_simulated_gateway_ingress(_env())

        self.assertTrue(first.passed)
        self.assertTrue(second.passed)

    async def test_synthetic_proxy_propagates_and_writes_ingress_receipt(self) -> None:
        captured: list[dict[str, object]] = []

        async def observe(frame: dict[str, object]) -> None:
            captured.append(dict(frame))

        with TemporaryDirectory(prefix="qq-ingress-receipt-") as raw:
            env = _env()
            env["CHATCOPILOT_INGRESS_RECEIPT_DIR"] = raw
            receipt = await run_simulated_gateway_ingress(
                env,
                downstream_observer=observe,
            )
            self.assertTrue(receipt.passed)
            self.assertEqual(len(captured), 1)
            frame = captured[0]
            normalized = normalized_onebot_text(frame)
            self.assertIsNotNone(normalized)
            content, _segment_count = normalized or ("", 0)
            match = consume_ingress_receipt(
                Path(raw),
                platform="qq",
                chat_kind="group",
                chat_id=str(frame["group_id"]),
                actor_id=str(frame["user_id"]),
                content=content,
            )

        self.assertEqual(match.status, "matched")
        self.assertIsNotNone(match.receipt)
        decision = dict((match.receipt or {}).get("decision") or {})
        self.assertEqual(decision.get("outcome"), "forward")
        self.assertEqual(decision.get("code"), "group_mention_matched")

    def test_proxy_config_can_be_built_from_immutable_mapping(self) -> None:
        config = _ProxyConfig(_env())

        self.assertEqual(config.bot_qq, _BOT)
        self.assertEqual(config.user_ids, frozenset({_USER}))
        self.assertEqual(config.group_ids, frozenset({_GROUP}))
        self.assertTrue(config.require_at)


if __name__ == "__main__":
    unittest.main()
