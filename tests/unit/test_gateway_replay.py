"""Real three-layer composition with only model/Provider responses controlled."""
from dataclasses import replace
import base64
import hashlib
import json

import pytest

from chatcopilot.core.llm_client import ChatResult
from chatcopilot.evals.agent_case import case_identity, evaluation_cases
from chatcopilot.evals.evaluation_runtime import load_evaluation_runtime
from chatcopilot.evals.frozen_agent_scoring import score
from chatcopilot.evals import gateway_replay
from chatcopilot.evals.trial_capture import capture


@pytest.fixture
def runtime(monkeypatch):
    runtime = load_evaluation_runtime("lingye-copilot-qq", load_local_environment=False)
    runtime = replace(runtime, runtime_id="native")
    monkeypatch.setattr(gateway_replay, "load_evaluation_runtime", lambda _: runtime)
    class Calls(list):
        responses = []
    calls = Calls()
    class Model:
        def __init__(self, config):
            self.model = "controlled-model"
            self.config = config
        def chat(self, **kwargs):
            calls.append(kwargs)
            if calls.responses:
                return calls.responses.pop(0)
            return ChatResult(content="actual reply", tool_calls=[], usage={})
        def close(self):
            pass
    monkeypatch.setattr("chatcopilot.agent.runtime.LLMClient", Model)
    return calls


def declaration(**kwargs):
    return {"schema": "agentstrata.agent-case/v1", "title": "Runtime reply", "input": "Reply to this message",
            "expected_behavior": "actual reply", "role": "user", "channel_kind": "group",
            "runtime_replay": True, "allowed_tools": [], "fixtures": {},
            "assertions": [{"kind": "final_contains", "value": "actual reply"}], "semantic": False, **kwargs}


def execute(tmp_path, data):
    case = evaluation_cases({"snapshot_id": case_identity(data), "case": data})[0]
    with capture():
        result = gateway_replay.run(case, bot="fixture", workspace_root=tmp_path)
    return result, score(case, result)[0]


def test_product_gateway_application_and_agent_execute(tmp_path, runtime):
    result, verdict = execute(tmp_path, declaration())
    assert verdict.passed, result
    assert runtime
    replay = result.evidence[0]["runtime_replay"]
    assert replay["layers"] == ["gateway", "application", "agent"]
    assert any(s["operation"] == "application.session" for s in replay["stages"])
    assert any(s["operation"] == "application.exchange" for s in replay["stages"])
    assert replay["receipts"][-1]["stage"] == "provider_acknowledged"
    assert not replay["production_delivery"]


def test_admission_denial_never_calls_model(tmp_path, runtime):
    result, verdict = execute(tmp_path, declaration(channel_kind="private", admission="denied", assertions=[{"kind": "admission_denied"}]))
    assert verdict.passed
    assert result.stop_reason == "denied" and not runtime
    assert result.evidence[0]["runtime_replay"]["layers"] == ["gateway"]


def test_invented_context_is_not_replayed(tmp_path, runtime):
    with pytest.raises(ValueError, match="context"):
        execute(tmp_path, declaration(context="Search already found a URL"))
    assert not runtime


def test_real_image_tool_uses_gateway_bound_delivery(tmp_path, runtime):
    from test_image_delivery_fixture import PNG
    url = "https://fixture.invalid/image.png"
    runtime.responses = [ChatResult(content="", usage={}, tool_calls=[{
        "id": "image-call", "type": "function", "function": {"name": "send_image_urls_to_user",
        "arguments": json.dumps({"urls": [url]})}}])]
    result, verdict = execute(tmp_path, declaration(input=f"Send this image: {url}",
        allowed_tools=["send_image_urls_to_user"],
        external_fixtures={"http": {url: {"content_type": "image/png", "body_base64": base64.b64encode(PNG).decode()}},
                           "delivery": "acknowledged"},
        assertions=[{"kind": "tool_called", "name": "send_image_urls_to_user"}, {"kind": "image_delivered"}]))
    assert verdict.passed, (verdict, result)
    assert result.tool_calls[0]["ok"]
    receipts = result.evidence[0]["runtime_replay"]["receipts"]
    image = [row for row in receipts if "image" in row["segments"] and row["stage"] == "provider_acknowledged"]
    assert image and image[0]["run_id"] and image[0]["session_id"]


def test_replay_uses_actual_actor_policy_projection(tmp_path, runtime, monkeypatch):
    import chatcopilot.application.actor_runtime as actors
    from chatcopilot.contracts.tool_packs import ToolPackPolicy
    marker = "ACTOR_PROJECTION_SENTINEL"
    original = actors._prompt_projection
    seen = []
    def project(*args, **kwargs):
        seen.append(kwargs)
        policies, skills = original(*args, **kwargs)
        return (*policies, ToolPackPolicy(id="replay.sentinel", content=marker)), skills
    monkeypatch.setattr(actors, "_prompt_projection", project)
    execute(tmp_path, declaration())
    assert seen
    assert marker in json.dumps(runtime[0]["messages"], ensure_ascii=False)


def test_original_image_traverses_materialization_and_backend_dispatch(tmp_path, runtime):
    from chatcopilot.evals.case_images import CaseImages
    from test_image_delivery_fixture import PNG
    images = CaseImages(tmp_path / "images")
    reference = {"scope": "replay-input", "sha256": hashlib.sha256(PNG).hexdigest(), "media_type": "image/png"}
    images.import_chunk(reference, 0, base64.b64encode(PNG).decode(), len(PNG))
    data = declaration(resources=[reference])
    case = evaluation_cases({"snapshot_id": case_identity(data), "case": data, "image_root": str(images.root)})[0]
    with capture():
        result = gateway_replay.run(case, bot="fixture", workspace_root=tmp_path / "trial")
    verdict, evidence = score(case, result)
    assert verdict.passed, evidence
    dispatched = [event for event in result.events if event["type"] == "InputResourcesDispatched"]
    assert dispatched and dispatched[0]["resources"][0]["sha256"] == reference["sha256"]


def test_codex_question_reply_bypasses_waiting_conversation_via_real_gateway(tmp_path, monkeypatch):
    """Only process/auth/OneBot peers are controlled; no actor/session substitute."""
    import asyncio
    import re
    import subprocess
    from chatcopilot.core.model_credentials import AccessCredential
    from chatcopilot.evals.image_delivery_fixture import OneBotFixtureConnection
    from chatcopilot.contracts.interactions import OperatorResponder
    selected = load_evaluation_runtime("lingye-copilot-qq", load_local_environment=False)
    monkeypatch.setattr(gateway_replay, "load_evaluation_runtime", lambda _: selected)
    monkeypatch.setattr("chatcopilot.agent.runtimes.codex.access_credential", lambda *a, **k: AccessCredential("fixture", "fixture-account", 1))
    executable = tmp_path / "codex"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    monkeypatch.setenv("CHATCOPILOT_CODEX_BIN", str(executable))
    holder = {}
    build = gateway_replay.build_gateway_runtime_host
    def capture_host(*args, **kwargs):
        holder["host"] = build(*args, **kwargs)
        return holder["host"]
    monkeypatch.setattr(gateway_replay, "build_gateway_runtime_host", capture_host)
    original_send = OneBotFixtureConnection.send
    async def send(self, raw):
        await original_send(self, raw)
        request = json.loads(raw)
        text = json.dumps(request.get("params", {}).get("message", []), ensure_ascii=False)
        found = re.search(r"interaction_[a-f0-9]+", text)
        if found:
            await self.queue.put(json.dumps({"post_type": "message", "message_type": "group", "self_id": "10001",
                "message_id": "reply-2", "group_id": "30003", "user_id": "20002",
                "sender": {"user_id": "20002", "nickname": "Evaluation"},
                "message": [{"type": "at", "data": {"qq": "10001"}},
                    {"type": "text", "data": {"text": "答复 " + found.group() + " selected"}}]}))
    monkeypatch.setattr(OneBotFixtureConnection, "send", send)
    def native_turn(command, **kwargs):
        kwargs["on_thread"]("fixture-thread")
        notify = kwargs["on_notification"]
        notify("turn/started", {"threadId": "fixture-thread", "turn": {"id": "fixture-turn"}})
        resolution = kwargs["on_request"]("item/tool/requestUserInput", {"threadId": "fixture-thread",
            "turnId": "fixture-turn", "itemId": "question", "questions": [{"id": "q", "header": "Choice", "question": "Which?"}]})
        assert resolution == {"answers": {"q": {"answers": ["selected"]}}}
        rows = holder["host"].coordinator.interactions.list(OperatorResponder("fixture", "fixture"))
        holder["interaction"] = rows[0]
        notify("item/completed", {"threadId": "fixture-thread", "turnId": "fixture-turn",
            "item": {"id": "answer", "type": "agentMessage", "phase": "final_answer", "text": "actual reply"}})
        notify("turn/completed", {"threadId": "fixture-thread", "turn": {"id": "fixture-turn", "status": "completed"}})
        return subprocess.CompletedProcess(command, 0, "", "")
    monkeypatch.setattr("chatcopilot.agent.runtimes.codex.run_app_server", native_turn)
    # A broken bypass must fail quickly, not hold a test for the interaction TTL.
    execute_async = gateway_replay._execute
    async def bounded(*args, **kwargs):
        return await asyncio.wait_for(execute_async(*args, **kwargs), timeout=15)
    monkeypatch.setattr(gateway_replay, "_execute", bounded)
    result, verdict = execute(tmp_path, declaration())
    assert verdict.passed, result
    assert holder["interaction"]["state"] == "answered"
    assert holder["interaction"]["responder"]["kind"] == "actor"
    assert not result.evidence[0]["runtime_replay"]["production_delivery"]
