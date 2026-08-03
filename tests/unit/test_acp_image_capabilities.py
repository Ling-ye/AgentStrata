from __future__ import annotations

import unittest
from types import SimpleNamespace

from chatcopilot.middleware.acp.server import AcpChatAgent


class AcpImageCapabilitiesTests(unittest.IsolatedAsyncioTestCase):
    async def test_initialize_advertises_configured_image_prompt_capability(
        self,
    ) -> None:
        for features, expected in (
            (("chat.image_inputs",), True),
            ((), False),
        ):
            with self.subTest(features=features):
                agent = AcpChatAgent.__new__(AcpChatAgent)
                agent._runtime = SimpleNamespace(tool_features=features)

                response = await agent.initialize(protocol_version=1)

                self.assertIs(
                    response.agent_capabilities.prompt_capabilities.image,
                    expected,
                )


if __name__ == "__main__":
    unittest.main()
