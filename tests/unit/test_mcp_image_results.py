import base64
import json
from types import SimpleNamespace

import pytest

from chatcopilot.agent.mcp.serialization import _serialize_call_result
from chatcopilot.agent.mcp.stateless import _serialize_stateless_result
from chatcopilot.agent.mcp.tool_wrapper import _wrap_remote_tool
from chatcopilot.agent.runtimes.dynamic_tools import DynamicToolBridge
from chatcopilot.contracts.runtime import McpServerConfig
from chatcopilot.contracts.tools import ToolContext
from chatcopilot.core.image_content import ImageContentError
from chatcopilot.core.llm_client import _expand_local_image_blocks
from chatcopilot.core.responses_client import responses_input
from chatcopilot.core.tool_images import image_content_blocks, validate_tool_images


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a5f8AAAAASUVORK5CYII=")


def raw_result():
    return {"isError": False, "content": [
        {"type": "text", "text": "Current window"},
        {"type": "image", "data": base64.b64encode(PNG).decode(), "mimeType": "image/png"},
    ]}


def materialize(tmp_path):
    response = _serialize_stateless_result(raw_result(), max_chars=1000)
    provider = SimpleNamespace(call_tool=lambda *args: response)
    remote = SimpleNamespace(name="Screenshot", description="Observe", inputSchema={"type": "object"})
    tool = _wrap_remote_tool(McpServerConfig(id="desktop", risk="interactive"), provider, remote, "desktop_Screenshot")
    return tool.handler({}, ToolContext(workspace_root=tmp_path))


def test_stateful_and_stateless_images_are_identical_and_not_text():
    raw = raw_result()
    first = _serialize_stateless_result(raw, max_chars=1000)
    second = _serialize_call_result(SimpleNamespace(isError=False,
        content=[SimpleNamespace(**item) for item in raw["content"]]), max_chars=1000)
    assert first == second
    assert first.images[0].data == PNG
    assert base64.b64encode(PNG).decode() not in first.text
    assert json.loads(first.text)["content"][1]["sha256"] == first.images[0].sha256


def test_remote_pixels_require_workspace_and_remain_private(tmp_path):
    result = materialize(tmp_path)
    assert result.ok and len(result.images) == 1
    image = result.images[0]
    from pathlib import Path
    path = Path(image.path)
    assert path.is_relative_to(tmp_path) and path.read_bytes() == PNG
    assert path.stat().st_mode & 0o777 == 0o600
    payload = json.dumps(result.to_llm_payload())
    assert image.path not in payload and base64.b64encode(PNG).decode() not in payload
    validate_tool_images(result.images, tmp_path)
    with pytest.raises(ValueError, match="escaped"):
        validate_tool_images(result.images, tmp_path / "other-actor")
    path.write_bytes(b"changed")
    with pytest.raises(ImageContentError):
        validate_tool_images(result.images, tmp_path)


def test_invalid_image_does_not_become_a_successful_observation():
    raw = raw_result()
    raw["content"][1]["data"] = base64.b64encode(b"not an image").decode()
    with pytest.raises(ImageContentError):
        _serialize_stateless_result(raw, max_chars=1000)


def test_tool_batch_projects_images_after_all_tool_receipts(tmp_path):
    result = materialize(tmp_path)
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "a", "type": "function", "function": {"name": "Screenshot", "arguments": "{}"}},
            {"id": "b", "type": "function", "function": {"name": "state", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "a", "content": [
            {"type": "text", "text": "screenshot received"}, *image_content_blocks(result.images)]},
        {"role": "tool", "tool_call_id": "b", "content": "state received"},
    ]
    projected = _expand_local_image_blocks(messages)
    assert [item["role"] for item in projected] == ["assistant", "tool", "tool", "user"]
    assert projected[1]["content"] == "screenshot received"
    assert projected[-1]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert messages[1]["content"][1]["type"] == "local_image"
    _, response_items = responses_input(projected)
    assert response_items[-1]["content"][1]["type"] == "input_image"
    native = DynamicToolBridge._response(result.to_llm_payload(), images=result.images)
    assert native["contentItems"][1]["type"] == "inputImage"
    assert native["contentItems"][1]["imageUrl"] == projected[-1]["content"][1]["image_url"]["url"]
