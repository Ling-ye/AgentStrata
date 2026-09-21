"""Credential, migration and configuration boundaries without external inference."""

import base64
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

from chatcopilot.core import model_credentials as credentials
from chatcopilot.core.codex_extensions import read_extensions
from chatcopilot.core.codex_extensions import validate_extension_ownership, extension_digest
from chatcopilot.gateway.state_store import GatewayStateStore
from chatcopilot.contracts.execution import (
    Capability,
    CapabilitySnapshot,
    HostRuntimePolicy,
    TranscriptSnapshot,
)


def token(account="fixture-account", *, expires=1):
    payload = {"exp": expires, "https://api.openai.com/auth": {"chatgpt_account_id": account}}
    return (
        "fixture."
        + base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        + ".fixture"
    )


def install(tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir(mode=0o700)
    path = staging / "auth.json"
    path.write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "OPENAI_API_KEY": None,
                "tokens": {
                    "access_token": token(),
                    "refresh_token": "private-refresh",
                    "id_token": "fixture",
                    "account_id": "fixture-account",
                },
            }
        )
    )
    path.chmod(0o600)
    root = tmp_path / "authority"
    epoch = credentials.install_login_credential(root, "main", staging)
    return root, epoch


def test_concurrent_refresh_is_single_owner_and_does_not_change_identity(tmp_path, monkeypatch):
    root, epoch = install(tmp_path)
    calls = []

    def refresh(*args, **kwargs):
        calls.append(kwargs)
        time.sleep(0.02)
        return SimpleNamespace(
            status_code=200, json=lambda: {"access_token": token(expires=time.time() + 3600)}
        )

    monkeypatch.setattr("requests.post", refresh)
    with ThreadPoolExecutor(max_workers=8) as pool:
        access = list(pool.map(lambda _: credentials.access_credential(root), range(8)))
    assert len(calls) == 1
    assert {item.identity_epoch for item in access} == {epoch}
    assert "private-refresh" not in repr(access)
    with credentials.credential_lock(root, "main", blocking=False):
        pass  # Inference does not retain the refresh lease.


def test_refresh_account_drift_does_not_overwrite_authority(tmp_path, monkeypatch):
    root, _ = install(tmp_path)
    path = credentials.authoritative_auth_path(root, "main")
    before = path.read_bytes()
    monkeypatch.setattr(
        "requests.post",
        lambda *a, **k: SimpleNamespace(
            status_code=200,
            json=lambda: {"access_token": token("other-account", expires=time.time() + 3600)},
        ),
    )
    with pytest.raises(credentials.CredentialError, match="account_identity_changed"):
        credentials.access_credential(root)
    assert path.read_bytes() == before


def test_policy_and_transcript_are_deeply_frozen():
    caps, grants = {"files"}, ["apps"]
    policy = HostRuntimePolicy(native_capabilities=caps, extension_grants=grants)
    caps.add("shell")
    grants.append("unreviewed")
    assert policy.native_capabilities == {"files"} and policy.extension_grants == ("apps",)
    view = TranscriptSnapshot(({"role": "user", "content": [{"text": "one"}]},), "host_history")
    with pytest.raises(TypeError):
        view.messages[0]["content"][0]["text"] = "changed"


def test_tool_schema_changes_invalidate_capability_digest():
    def snapshot(schema):
        return CapabilitySnapshot(
            (Capability("host:tool", "host", host_tool_name="tool", schema_digest=schema),)
        )

    assert snapshot("a").fingerprint != snapshot("b").fingerprint


def test_extension_file_cannot_override_policy_or_embed_header_secret(tmp_path):
    path = tmp_path / "extensions.toml"
    for text in (
        'approval_policy="never"',
        '[mcp_servers.service]\nhttp_headers={Authorization="private"}',
    ):
        path.write_text(text)
        with pytest.raises(ValueError):
            read_extensions(path)
    path.write_text(
        '[mcp_servers.service]\nurl="https://service.example/mcp"\nbearer_token_env_var="SERVICE_TOKEN"'
    )
    assert read_extensions(path) == path.read_text()
    with pytest.raises(ValueError, match="both"):
        validate_extension_ownership(path.read_text(), {"service"})
    assert extension_digest(path.read_text()) == extension_digest("# comment\n" + path.read_text())
    path.write_text("[apps._default]\nenabled = true\n")
    with pytest.raises(ValueError, match="explicit per-app"):
        read_extensions(path)
    path.write_text("[apps._default]\nenabled = false\n[apps.fixture]\nenabled = true\n")
    assert read_extensions(path) == path.read_text()
    from chatcopilot.core.codex_extensions import managed_extension_config

    assert "enabled = false" in managed_extension_config("")


def test_provider_auth_error_cannot_echo_handoff_secret(tmp_path, monkeypatch):
    from chatcopilot.agent.runtimes.codex import CodexRuntimeAdapter
    from chatcopilot.core.config import ChatConfig, LLMConfig
    from chatcopilot.contracts.agent import AgentTask
    from chatcopilot.contracts.runtime_adapter import RuntimeOpenRequest
    from tests.prompt_plan_fixture import prompt_plan, runtime_route

    secret = "private-handoff-fixture"
    config = ChatConfig(
        llm=LLMConfig(provider="openai", api="openai_responses", api_key=secret)
    )
    route = runtime_route("codex", config.llm)
    backend = CodexRuntimeAdapter(
        route=route,
        tool_names=set(),
        runtime_config=config,
    )
    ref = backend.open_session(
        RuntimeOpenRequest(
            session_id="test",
            prompt_plan=prompt_plan("host"),
            route=route,
            options={"workspace_root": tmp_path, "runtime_state_root": tmp_path / "state"},
        )
    )
    monkeypatch.setattr(
        "chatcopilot.external_tools.codex_cli.command._resolve_executable",
        lambda *a: "/usr/bin/true",
    )

    def fail(*args, **kwargs):
        raise RuntimeError("provider echoed " + secret)

    monkeypatch.setattr("chatcopilot.agent.runtimes.codex.run_app_server", fail)
    events = []
    try:
        result = backend.stream_turn(ref, AgentTask("hello"), on_event=events.append)
        assert result.stop_reason == "runtime_error"
        assert secret not in repr(events) and secret not in repr(result)
    finally:
        backend.close_session(ref)


def test_unlinked_sqlite_sidecar_is_not_confused_with_multiple_hardlinks(tmp_path, monkeypatch):
    import os
    import stat
    from chatcopilot.gateway.state_store import _validate_sqlite_files, GatewayStateError

    store = GatewayStateStore(tmp_path / "state")
    sidecar = Path(str(store.database_path) + "-wal")
    original = Path.lstat
    links = [0]

    def metadata(path):
        if path == sidecar:
            return SimpleNamespace(
                st_mode=stat.S_IFREG | 0o600, st_uid=os.getuid(), st_nlink=links[0]
            )
        return original(path)

    monkeypatch.setattr(Path, "lstat", metadata)
    _validate_sqlite_files(store.database_path)
    links[0] = 2
    with pytest.raises(GatewayStateError, match="one hard link"):
        _validate_sqlite_files(store.database_path)


def test_completed_turn_callback_cannot_enter_recreated_tool_bridge(tmp_path, monkeypatch):
    import subprocess
    from chatcopilot.agent.runtimes.codex import CodexRuntimeAdapter
    from chatcopilot.agent.tools.executor import ToolExecutor
    from chatcopilot.core.config import ChatConfig, LLMConfig
    from chatcopilot.contracts.agent import AgentTask
    from chatcopilot.contracts.runtime_adapter import RuntimeOpenRequest
    from chatcopilot.contracts.cancellation import CancellationRequested
    from chatcopilot.contracts.tools import ToolDef, ToolResult
    from tests.prompt_plan_fixture import prompt_plan, runtime_route

    effects, callbacks, accepted_late = [], [], []
    tool = ToolDef(
        "save",
        "Save",
        {"type": "object", "properties": {}},
        {"type": "object"},
        lambda a, c: (effects.append(c.request_text), ToolResult(ok=True, summary="saved"))[1],
        access="member",
    )
    config = ChatConfig(
        llm=LLMConfig(provider="openai", api="openai_responses", api_key="fixture")
    )
    route = runtime_route("codex", config.llm)
    backend = CodexRuntimeAdapter(
        route=route,
        tool_names={"save"},
        tools=(tool,),
        tool_executor=ToolExecutor(tools=[tool], caller_role_hint="owner"),
        runtime_config=config,
    )
    ref = backend.open_session(
        RuntimeOpenRequest(
            session_id="test",
            prompt_plan=prompt_plan("host"),
            route=route,
            allowed_tool_names=frozenset({"save"}),
            options={"workspace_root": tmp_path, "runtime_state_root": tmp_path / "state"},
        )
    )
    monkeypatch.setattr(
        "chatcopilot.external_tools.codex_cli.command._resolve_executable",
        lambda *a: "/usr/bin/true",
    )

    def run(command, **kwargs):
        kwargs["on_thread"]("thread")
        notify = kwargs["on_notification"]
        notify("turn/started", {"threadId": "thread", "turn": {"id": "turn"}})
        if callbacks:
            try:
                callbacks[0](
                    "item/tool/call",
                    {
                        "threadId": "thread",
                        "turnId": "old-turn",
                        "callId": "late",
                        "namespace": "agentstrata",
                        "tool": "save",
                        "arguments": {},
                    },
                )
            except CancellationRequested:
                pass
            else:
                accepted_late.append(True)
        callbacks.append(kwargs["on_request"])
        notify(
            "item/completed",
            {
                "threadId": "thread",
                "turnId": "turn",
                "item": {
                    "id": "final",
                    "type": "agentMessage",
                    "phase": "final_answer",
                    "text": "done",
                },
            },
        )
        notify(
            "turn/completed", {"threadId": "thread", "turn": {"id": "turn", "status": "completed"}}
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("chatcopilot.agent.runtimes.codex.run_app_server", run)
    try:
        assert (
            backend.stream_turn(ref, AgentTask("first"), on_event=lambda _: None).stop_reason
            == "end_turn"
        )
        backend._reset_session_relay(backend._resolve(ref))
        assert (
            backend.stream_turn(ref, AgentTask("second"), on_event=lambda _: None).stop_reason
            == "end_turn"
        )
        assert effects == [] and accepted_late == []
    finally:
        backend.close_session(ref)
