"""Real image handler, file sender, ChannelRuntime, OneBot codec and wire receipts."""
import base64

import pytest

from chatcopilot.agent.tools.builtin.workspace_tools import TOOLS
from chatcopilot.agent.tools.executor import ToolExecutor
from chatcopilot.core.workspace_runtime import Workspace, MiddlewareWorkspaceService
from chatcopilot.application.execution_scope import execution_scope
from chatcopilot.evals.image_delivery_fixture import ImageDeliveryFixture
from chatcopilot.evals.agent_case import validate_case, capabilities, SCHEMA, case_identity, evaluation_cases
from chatcopilot.evals.frozen_agent_scoring import score
from chatcopilot.evals.models import TrialObservation

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")
URL = "https://example.com/image.png"


def declaration(outcome="acknowledged", body=PNG):
    return {"schema": SCHEMA, "title": "Send an image", "input": "Send the linked image",
            "expected_behavior": "Image delivered", "channel_kind": "group",
            "allowed_tools": ["send_image_urls_to_user"],
            "assertions": [{"kind": "image_delivered"}],
            "external_fixtures": {"http": {URL: {"body_base64": base64.b64encode(body).decode()}}, "delivery": outcome}}


@pytest.mark.parametrize("outcome,expected", [("acknowledged", True), ("rejected", False), ("unknown", False), ("incomplete", False)])
def test_real_delivery_chain_reaches_only_fake_websocket(tmp_path, outcome, expected):
    workspace = Workspace(root=tmp_path / "workspace", chat_kind="group", chat_id="30003", user_id="20002").ensure()
    service = MiddlewareWorkspaceService(workspace=workspace, workspace_root=tmp_path,
        execution_scope=execution_scope("owner", workspace.root, (workspace.root,)))
    case = validate_case(declaration(outcome))
    with ImageDeliveryFixture(tmp_path, workspace, case) as fixture:
        executor = ToolExecutor(tools=list(TOOLS), file_sender=fixture.sender, workspace_service=service,
                                caller_role_hint="owner")
        result = executor.execute("send_image_urls_to_user", {"urls": [URL], "message": "image"})
        evidence = fixture.evidence()
    assert result.ok is expected, result.error
    assert len(evidence["actions"]) == 1
    action = evidence["actions"][0]
    assert action["params"]["group_id"] == "30003"
    images = [s for s in action["params"]["message"] if s["type"] == "image"]
    assert base64.b64decode(images[0]["data"]["file"].removeprefix("base64://")) == PNG
    evaluated = evaluation_cases({"snapshot_id": case_identity(case), "case": case})[0]
    facts, _ = score(evaluated, TrialObservation(evidence=(evidence,)))
    assert facts.passed is expected
    if not expected:
        assert result.error_code == "image_delivery_unconfirmed"


def test_invalid_image_stops_before_any_outbound(tmp_path):
    workspace = Workspace(root=tmp_path / "workspace", chat_kind="group", chat_id="30003", user_id="20002").ensure()
    service = MiddlewareWorkspaceService(workspace=workspace, workspace_root=tmp_path,
        execution_scope=execution_scope("owner", workspace.root, (workspace.root,)))
    with ImageDeliveryFixture(tmp_path, workspace, validate_case(declaration(body=b"not an image"))) as fixture:
        result = ToolExecutor(tools=list(TOOLS), file_sender=fixture.sender, workspace_service=service,
                              caller_role_hint="owner").execute("send_image_urls_to_user", {"urls": [URL]})
        assert not result.ok and result.error_code == "image_download_failed"
        assert not fixture.evidence()["actions"]


def test_delivery_tools_require_explicit_fixture_and_are_advertised():
    case = declaration()
    case.pop("external_fixtures")
    with pytest.raises(ValueError, match="fixture"):
        validate_case(case)
    assert "send_image_urls_to_user" in capabilities()["tools"]


@pytest.mark.parametrize("value", [{"http": {1: {}}}, {"http": {URL: {"body_base64": {}}}}, {"delivery": []}])
def test_malformed_fixture_is_a_definition_error(value):
    from chatcopilot.evals.image_delivery_fixture import validate_fixtures
    with pytest.raises(ValueError):
        validate_fixtures(value)


def test_file_sender_rejects_oversize_instead_of_sending_truncated_bytes(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from chatcopilot.application import file_delivery
    dispatch = Mock()
    monkeypatch.setattr(file_delivery, "read_bytes", lambda path, limit: b"x" * limit)
    sender = file_delivery.create_file_sender(SimpleNamespace(root=tmp_path), dispatch)
    with pytest.raises(ValueError, match="exceeds"):
        sender([str(tmp_path / "large.bin")], "")
    dispatch.assert_not_called()
