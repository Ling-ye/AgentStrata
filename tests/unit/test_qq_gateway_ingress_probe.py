from __future__ import annotations

import json
import unittest

from chatcopilot.evals.qq_ingress_probe import run_simulated_gateway_ingress


_TOKEN = "probe_" + ("x" * 32)
_BOT = "1" + "0001"


def _env() -> dict[str, str]:
    return {
        "QQ_ACCESS_TOKEN": _TOKEN,
        "QQ_ACCOUNT": _BOT,
    }


class SimulatedGatewayIngressTests(unittest.IsolatedAsyncioTestCase):
    async def test_current_channel_accepts_explicit_at_and_drops_missing_at(self) -> None:
        receipt = await run_simulated_gateway_ingress(_env())

        self.assertTrue(receipt.passed)
        self.assertTrue(receipt.upstream_authenticated)
        self.assertTrue(receipt.positive_forwarded)
        self.assertTrue(receipt.negative_dropped)
        self.assertEqual(receipt.mode, "hermetic_loopback")
        self.assertEqual(len(receipt.positive_frame_sha256), 64)
        self.assertEqual(len(receipt.negative_frame_sha256), 64)

    async def test_evidence_contains_no_identity_token_or_message_text(self) -> None:
        receipt = await run_simulated_gateway_ingress(_env())

        serialized = json.dumps(receipt.to_evidence(), sort_keys=True)
        for private_value in (_TOKEN, _BOT, "ingress-probe"):
            self.assertNotIn(private_value, serialized)

    async def test_ephemeral_listeners_are_released_between_runs(self) -> None:
        first = await run_simulated_gateway_ingress(_env())
        second = await run_simulated_gateway_ingress(_env())

        self.assertTrue(first.passed)
        self.assertTrue(second.passed)

    async def test_downstream_observer_receives_only_positive_frame(self) -> None:
        captured: list[dict[str, object]] = []

        async def observe(frame: dict[str, object]) -> None:
            captured.append(dict(frame))

        receipt = await run_simulated_gateway_ingress(
            _env(),
            downstream_observer=observe,
        )

        self.assertTrue(receipt.passed)
        self.assertEqual(len(captured), 1)
        message = captured[0].get("message")
        self.assertIsInstance(message, list)
        self.assertEqual((message or [])[0].get("type"), "at")



if __name__ == "__main__":
    unittest.main()
