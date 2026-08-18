from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from chatcopilot.agent.backends.codex import CodexAgentBackend
from chatcopilot.agent.session import AgentSession
from chatcopilot.agent.tools.executor import ToolExecutor
from chatcopilot.contracts.agent import (
    AgentTask,
    InputResourcesDispatched,
    ResourceRef,
)
from chatcopilot.core.llm_client import LLMClient


_PNG_BYTES = b"\x89PNG\r\n\x1a\nmultimodal-test"


class _Limiter:
    @contextmanager
    def slot(self):
        yield


class _Completions:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        message = SimpleNamespace(
            content="image received",
            reasoning_content="",
            tool_calls=None,
        )
        choice = SimpleNamespace(message=message, finish_reason="stop")
        return SimpleNamespace(choices=[choice], usage=None)


def _image_resource(path: Path) -> ResourceRef:
    data = path.read_bytes()
    return ResourceRef(
        name=path.name,
        path=str(path),
        kind="file",
        media_type="image/png",
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )


def _llm_client(completions: _Completions) -> LLMClient:
    client = object.__new__(LLMClient)
    client._cfg = SimpleNamespace(model="vision-test")
    client._client = SimpleNamespace(
        chat=SimpleNamespace(completions=completions)
    )
    client._limiter = _Limiter()
    return client


def test_native_expands_image_only_at_request_boundary(tmp_path: Path) -> None:
    image_path = tmp_path / "input.png"
    image_path.write_bytes(_PNG_BYTES)
    resource = _image_resource(image_path)
    completions = _Completions()
    session = AgentSession(
        session_id="multimodal-native",
        llm=_llm_client(completions),
        executor=ToolExecutor(tools=[]),
        tools_schema=[],
        system_baseline="system",
        stream_first_turn=False,
    )
    events: list[object] = []

    result = session.run_task(
        AgentTask(
            text="describe this image",
            resources=(resource,),
            metadata={"eval_turn": 3},
        ),
        on_event=events.append,
    )

    assert result.final_text == "image received"
    outbound_content = completions.calls[0]["messages"][1]["content"]
    image_blocks = [
        block for block in outbound_content if block.get("type") == "image_url"
    ]
    assert len(image_blocks) == 1
    assert image_blocks[0]["image_url"]["url"].startswith(
        "data:image/png;base64,"
    )

    transcript = json.dumps(session.snapshot_messages(), ensure_ascii=False)
    assert '"type": "local_image"' in transcript
    assert str(image_path) in transcript
    assert "data:image" not in transcript
    assert "base64," not in transcript
    dispatch = next(event for event in events if isinstance(event, InputResourcesDispatched))
    assert dispatch.backend == "native"
    assert dispatch.turn_index == 3
    assert dispatch.request_id
    assert dispatch.resources[0].sequence == 0
    assert dispatch.resources[0].media_type == "image/png"
    assert dispatch.resources[0].size_bytes == len(_PNG_BYTES)
    assert dispatch.resources[0].sha256 == hashlib.sha256(_PNG_BYTES).hexdigest()


def test_codex_new_and_resume_commands_attach_images(tmp_path: Path) -> None:
    image_path = tmp_path / "input.png"
    image_path.write_bytes(_PNG_BYTES)
    task = AgentTask(
        text="inspect the screenshot",
        resources=(_image_resource(image_path),),
    )
    routing = SimpleNamespace(
        code_command="codex exec",
        code_model="gpt-test",
        code_reasoning_effort="medium",
    )
    backend = CodexAgentBackend(
        tool_names=set(),
        runtime_config=SimpleNamespace(routing=routing),
    )
    state = SimpleNamespace(
        gateway_config=tmp_path / "gateway.json",
        allowed_tool_names=frozenset(),
        system_baseline="system",
        access_mode="workspace",
        workdir=tmp_path,
        native_session_id="",
    )
    image_paths = backend._image_paths(task)

    with mock.patch(
        "chatcopilot.agent.backends.codex.build_codex_command",
        side_effect=lambda **_kwargs: ["codex", "exec"],
    ):
        initial = backend._command(state, image_paths=image_paths)
        state.native_session_id = "thread-native-1"
        resumed = backend._command(state, image_paths=image_paths)
        without_image = backend._command(state)

    assert initial == ["codex", "exec", "--json", "--image", str(image_path)]
    assert resumed == [
        "codex",
        "exec",
        "--json",
        "resume",
        "thread-native-1",
        "--image",
        str(image_path),
        "-",
    ]
    assert without_image[-3:] == ["resume", "thread-native-1", "-"]
    assert str(image_path) in backend._prompt(state, task)
