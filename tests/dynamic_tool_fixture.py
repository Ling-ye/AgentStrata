"""Decode real App Server dynamic-tool response content for handler conformance tests."""

import json


def call_dynamic_tool(bridge, name, arguments):
    reply = bridge.call(
        {
            "namespace": "agentstrata",
            "threadId": "fixture-main",
            "tool": name,
            "arguments": arguments,
        },
        main_thread_id="fixture-main",
    )
    return json.loads(reply["contentItems"][0]["text"])
