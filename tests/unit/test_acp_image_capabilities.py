from __future__ import annotations
from tests.acp_runtime_fixture import make_acp_agent, acp_runtime

import unittest



class AcpImageCapabilitiesTests(unittest.IsolatedAsyncioTestCase):
    async def test_initialize_advertises_configured_image_prompt_capability(
        self,
    ) -> None:
        for features, expected in (
            (("chat.image_inputs",), True),
            ((), False),
        ):
            with self.subTest(features=features):
                agent = make_acp_agent()
                agent._runtime = acp_runtime(tool_features=features)

                response = await agent.initialize(protocol_version=1)

                self.assertIs(
                    response.agent_capabilities.prompt_capabilities.image,
                    expected,
                )


if __name__ == "__main__":
    unittest.main()
