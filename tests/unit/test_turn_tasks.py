import json
import os
import stat
import threading
from datetime import datetime, timezone
from pathlib import Path

from chatcopilot.core import observability_redaction
from chatcopilot.core.observability_redaction import (
    load_bounded_observability_json,
    omit_local_resource_paths,
    omit_private_reasoning_messages,
    redact_observability_payload,
)
from chatcopilot.middleware.runtime import tasks as task_runtime
from chatcopilot.middleware.runtime.tasks import (
    MAX_CONTEXT_ARTIFACT_BYTES,
    TurnTaskRecorder,
    complete_delegated_task,
)
from chatcopilot.core.workspace_runtime import Workspace


def _workspace(root: Path) -> Workspace:
    return Workspace(
        root=root,
        chat_kind="p2p",
        chat_id="oc_test",
        user_id="ou_test",
        user_name="Alice",
    ).ensure()


def test_turn_task_records_plain_answer_without_tools(tmp_path: Path) -> None:
    recorder = TurnTaskRecorder(
        workspace=_workspace(tmp_path / "ws"),
        session_id="sid-1",
        message_id="msg-1",
        user_text="你是谁，你能做什么",
    )

    recorder.finish(status="succeeded", progress="已完成回答。")
    payload = recorder.to_payload()

    assert payload["task_id"].startswith("task_")
    assert payload["description"] == "你是谁，你能做什么"
    assert payload["status"] == "succeeded"
    assert payload["submitter"] == "Alice"
    assert payload["tools"] == []
    assert payload["job_ids"] == []
    assert recorder.path.is_file()


def test_context_snapshot_is_redacted_bounded_private_and_summarized(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = _workspace(tmp_path / "ws")
    credential_value = "-".join(("test", "observability", "credential", "value"))
    json_credential_value = "".join(("plain", "credential123"))
    monkeypatch.setenv("TEST_CONTEXT_" + "API_" + "TOKEN", credential_value)
    monkeypatch.setenv("CHATCOPILOT_TOKEN_BUDGET", "200000")
    recorder = TurnTaskRecorder(
        workspace=workspace,
        session_id="sid-context",
        message_id="msg-context",
        user_text="inspect context",
    )

    recorder.context_snapshot(
        snapshot_id="ctx_span123",
        backend="native",
        model="test-model",
        iteration=0,
        session_messages=[
            {"role": "system", "content": f"token={credential_value}"},
            {"role": "system", "content": "private_key=TOPSECRET"},
            {"role": "user", "content": f"read {workspace.root}/notes.txt"},
            {
                "role": "assistant",
                "content": "public answer",
                "reasoning_content": "provider private chain of thought",
            },
            {
                "role": "tool",
                "content": json.dumps(
                    {
                        "api_" + "key": json_credential_value,
                        "token_budget": 200000,
                    },
                    separators=(",", ":"),
                ),
            },
            {
                "role": "tool",
                "content": (
                    '{"private_key":"TOPSECRET","cookie":"sessionid=TOPSECRET"}'
                ),
            },
            {
                "role": "user",
                "content": (
                    "-----BEGIN OPENSSH " + "PRIVATE KEY-----\nTOPSECRET\n"
                    "-----END OPENSSH PRIVATE KEY-----\n"
                    "eyJhbGciOiJIUzI1NiJ9.abcdefgh12345678.signature12345678"
                ),
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "inspect /tmp/private-resource.png"},
                    {
                        "type": "local_image",
                        "local_image": {
                            "path": "/tmp/private-resource.png",
                            "media_type": "image/png",
                            "size_bytes": 12,
                            "sha256": "a" * 64,
                        },
                    },
                ],
            },
        ],
        effective_messages=[
            {"role": "user", "content": f"Bearer {credential_value}"}
        ],
        tool_schemas=[{"type": "function", "function": {"name": "read_file"}}],
        resources=[
            {"sequence": 0, "media_type": "image/png", "size_bytes": 12, "sha256": "a" * 64}
        ],
        coverage="exact_model_input",
        omitted=[],
        context_kind="sliding_window",
        trace_id="trace-1",
        span_id="span123",
        estimated_tokens=42,
        model_selection={
            "model": "test-model",
            "reasoning_effort": "medium",
            "token_budget": 200000,
            "access_token": credential_value,
        },
    )
    recorder.record_event(
        "turn_error", {"message": f"Bearer {credential_value}"}
    )

    payload = recorder.to_payload()
    summary = payload["context_snapshots"][0]
    assert summary["snapshot_id"] == "ctx_span123"
    assert summary["coverage"] == "partial"
    assert summary["redacted"] is True
    assert summary["effective_message_count"] == 1
    artifact = recorder.path.parent / "contexts" / "ctx_span123.json"
    persisted = artifact.read_text(encoding="utf-8")
    assert credential_value not in persisted
    assert str(workspace.root) not in persisted
    assert "$WORKSPACE" in persisted
    assert "provider private chain of thought" not in persisted
    assert "reasoning_content" not in persisted
    assert json_credential_value not in persisted
    assert "TOPSECRET" not in persisted
    assert "eyJhbGciOiJIUzI1NiJ9" not in persisted
    assert "BEGIN OPENSSH PRIVATE KEY" not in persisted
    assert "/tmp/private-resource.png" not in persisted
    assert "$RESOURCE_aaaaaaaaaaaa" in persisted
    artifact_payload = json.loads(persisted)
    assert artifact_payload["model_selection"]["token_budget"] == 200000
    assert artifact_payload["model_selection"]["access_token"] == "[REDACTED]"
    assert "provider_private_reasoning" in artifact_payload["omitted"]
    assert artifact_payload["coverage"] == "partial"
    assert artifact_payload["sanitization"]["private_reasoning_omission_count"] == 1
    assert artifact_payload["sanitization"]["resource_path_omission_count"] == 1
    assert artifact.stat().st_mode & 0o777 == 0o600
    assert artifact.parent.stat().st_mode & 0o777 == 0o700
    events_text = (recorder.path.parent / "events.jsonl").read_text(encoding="utf-8")
    assert credential_value not in events_text
    events = [json.loads(line) for line in events_text.splitlines()]
    assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
    assert all(event["event_id"].startswith(f"{recorder.task_id}:") for event in events)


def test_oversized_context_snapshot_is_truncated_below_reader_limit(
    tmp_path: Path,
) -> None:
    recorder = TurnTaskRecorder(
        workspace=_workspace(tmp_path / "ws"),
        session_id="sid-large-context",
        message_id="msg-large-context",
        user_text="large context",
    )

    recorder.context_snapshot(
        snapshot_id="ctx_large",
        backend="native",
        model="test-model",
        iteration=0,
        session_messages=[
            {"role": "user", "content": "x" * (MAX_CONTEXT_ARTIFACT_BYTES + 1024)},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"function": {"name": "first", "arguments": "{}"}}],
            },
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"function": {"name": "second", "arguments": "{}"}}],
            },
        ],
        effective_messages=[],
        tool_schemas=[],
        resources=[],
        coverage="exact_model_input",
        omitted=[],
    )

    artifact = recorder.path.parent / "contexts" / "ctx_large.json"
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    assert artifact.stat().st_size <= MAX_CONTEXT_ARTIFACT_BYTES
    assert payload["capture_status"] == "truncated"
    assert payload["truncated"] is True
    assert payload["stored_bytes"] <= MAX_CONTEXT_ARTIFACT_BYTES
    assert payload["session_messages"][0]["char_count"] > MAX_CONTEXT_ARTIFACT_BYTES
    assert (
        payload["session_messages"][1]["message_sha256"]
        != payload["session_messages"][2]["message_sha256"]
    )


def test_task_size_fallback_preserves_context_artifact_index(
    tmp_path: Path,
) -> None:
    recorder = TurnTaskRecorder(
        workspace=_workspace(tmp_path / "ws"),
        session_id="sid-context-index",
        message_id="msg-context-index",
        user_text="preserve context index",
    )
    recorder.context_snapshot(
        snapshot_id="ctx_preserved",
        backend="native",
        model="test-model",
        iteration=0,
        session_messages=[{"role": "user", "content": "hello"}],
        effective_messages=[{"role": "user", "content": "hello"}],
        tool_schemas=[],
        resources=[],
        coverage="exact_model_input",
        omitted=[],
    )
    payload = json.loads(recorder.path.read_text(encoding="utf-8"))
    hostile = "m" * (9 * 1024 * 1024)
    payload["context_snapshots"][0]["model"] = hostile
    payload["llm_calls"] = [
        {
            "model": hostile,
            "context_snapshot_id": "ctx_preserved",
            "metadata": {"blob": hostile},
        }
    ]
    payload["input_resources"] = [
        {
            "backend": hostile,
            "request_id": "req-1",
            "metadata": {"blob": hostile},
            "resources": [{"media_type": hostile, "sha256": "a" * 64}],
        }
    ]
    hostile_metadata = {f"field_{index}": hostile for index in range(100)}
    payload["steps"] = [
        {
            "step_id": f"step-{index}",
            "type": "provider_event",
            "metadata": hostile_metadata,
        }
        for index in range(100)
    ]
    payload["hostile_metadata"] = {"blob": hostile}

    task_runtime._write_private_task_json(
        recorder.path.parent,
        task_runtime.TASK_FILENAME,
        payload,
    )

    persisted = json.loads(recorder.path.read_text(encoding="utf-8"))
    assert recorder.path.stat().st_size <= task_runtime.MAX_TASK_SUMMARY_BYTES
    assert persisted["summary_limits"]["payload_truncated"] is True
    assert persisted["summary_limits"]["context_snapshots_minimal"] is True
    assert persisted["summary_limits"]["context_snapshots_total"] == 1
    assert persisted["summary_limits"]["context_snapshots_retained"] == 1
    assert persisted["context_snapshots"][0]["snapshot_id"] == "ctx_preserved"
    assert len(persisted["context_snapshots"][0]["model"]) <= 128
    assert persisted["summary_limits"]["unknown_fields_omitted"] == 1
    assert persisted["llm_calls"][0]["context_snapshot_id"] == "ctx_preserved"
    assert len(persisted["llm_calls"][0]["model"]) <= 512
    assert len(persisted["input_resources"][0]["backend"]) <= 128
    assert (recorder.path.parent / "contexts" / "ctx_preserved.json").is_file()


def test_task_summary_caps_retain_latest_context_artifact_index(
    tmp_path: Path,
) -> None:
    from console.control import observability
    from console.control.instances import BotInstance

    workspace = _workspace(tmp_path / "ws")
    recorder = TurnTaskRecorder(
        workspace=workspace,
        session_id="sid-latest-context-index",
        message_id="msg-latest-context-index",
        user_text="retain latest context index",
    )
    recorder.context_snapshot(
        snapshot_id="ctx_5000",
        backend="native",
        model="latest-model",
        iteration=5000,
        session_messages=[{"role": "user", "content": "latest"}],
        effective_messages=[{"role": "user", "content": "latest"}],
        tool_schemas=[],
        resources=[],
        coverage="exact_model_input",
        omitted=[],
    )
    payload = json.loads(recorder.path.read_text(encoding="utf-8"))
    latest_context = payload["context_snapshots"][0]
    payload["context_snapshots"] = [
        {
            "snapshot_id": f"ctx_{index}",
            "backend": "native",
            "model": f"model-{index}",
            "iteration": index,
            "coverage": "exact_model_input",
            "capture_status": "captured",
            "captured_at": float(index),
        }
        for index in range(task_runtime.MAX_TASK_CONTEXT_SNAPSHOT_SUMMARIES)
    ] + [latest_context]
    payload["llm_calls"] = [
        {
            "model": f"model-{index}",
            "context_snapshot_id": f"ctx_{index}",
            "recorded_at": float(index),
        }
        for index in range(task_runtime.MAX_TASK_LLM_CALL_SUMMARIES + 1)
    ]
    payload["input_resources"] = [
        {
            "backend": "native",
            "request_id": f"req_{index}",
            "recorded_at": float(index),
            "resources": [],
        }
        for index in range(task_runtime.MAX_TASK_INPUT_RESOURCE_SUMMARIES + 1)
    ]

    task_runtime._write_private_task_json(
        recorder.path.parent,
        task_runtime.TASK_FILENAME,
        payload,
    )

    persisted = json.loads(recorder.path.read_text(encoding="utf-8"))
    limits = persisted["summary_limits"]
    contexts = persisted["context_snapshots"]
    context_ids = [item["snapshot_id"] for item in contexts]
    assert limits["context_snapshots_total"] == 5001
    assert limits["context_snapshots_retained"] == 5000
    assert limits["context_snapshots_truncated"] is True
    assert context_ids[0] == "ctx_1"
    assert context_ids[-1] == "ctx_5000"
    assert "ctx_0" not in context_ids
    assert limits["llm_calls_total"] == 1001
    assert limits["llm_calls_retained"] == 1000
    assert persisted["llm_calls"][0]["context_snapshot_id"] == "ctx_1"
    assert persisted["llm_calls"][-1]["context_snapshot_id"] == "ctx_1000"
    assert limits["input_resources_total"] == 501
    assert limits["input_resources_retained"] == 500
    assert persisted["input_resources"][0]["request_id"] == "req_1"
    assert persisted["input_resources"][-1]["request_id"] == "req_500"

    snapshot = observability.context_snapshot(
        BotInstance(
            instance_id="latest-context-index",
            bot_spec="",
            workspace_root=str(workspace.root),
        ),
        recorder.task_id,
        "ctx_5000",
    )
    assert snapshot is not None
    assert snapshot["snapshot_id"] == "ctx_5000"


def test_context_snapshot_persistence_failure_is_indexed_as_unavailable(
    tmp_path: Path,
) -> None:
    recorder = TurnTaskRecorder(
        workspace=_workspace(tmp_path / "ws"),
        session_id="sid-unavailable-context",
        message_id="msg-unavailable-context",
        user_text="context persistence failure",
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    (recorder.path.parent / "contexts").symlink_to(outside, target_is_directory=True)

    recorder.context_snapshot(
        snapshot_id="ctx_unavailable",
        backend="native",
        model="test-model",
        iteration=0,
        session_messages=[{"role": "user", "content": "sensitive prompt"}],
        effective_messages=[{"role": "user", "content": "sensitive prompt"}],
        tool_schemas=[],
        resources=[],
        coverage="exact_model_input",
        omitted=[],
        trace_id="trace-unavailable",
        span_id="span-unavailable",
    )

    summary = recorder.to_payload()["context_snapshots"][0]
    assert summary["capture_status"] == "unavailable"
    assert "persistence_failed" in summary["omitted"]
    assert not (outside / "ctx_unavailable.json").exists()
    events = [
        json.loads(line)
        for line in (recorder.path.parent / "events.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    unavailable_event = next(
        event for event in events if event["event"] == "context_snapshot"
    )
    assert unavailable_event["data"]["capture_status"] == "unavailable"


def test_llm_start_without_snapshot_creates_correlated_unavailable_summary(
    tmp_path: Path,
) -> None:
    recorder = TurnTaskRecorder(
        workspace=_workspace(tmp_path / "ws"),
        session_id="sid-missing-context",
        message_id="msg-missing-context",
        user_text="missing context event",
    )

    recorder.llm_call_started(
        backend="future-backend",
        model="future-model",
        iteration=0,
        trace_id="trace-future",
        span_id="span-future",
        input_message_count=4,
        input_estimated_tokens=321,
        tool_schema_count=2,
    )

    payload = recorder.to_payload()
    snapshot = payload["context_snapshots"][0]
    step = payload["steps"][0]
    assert snapshot["backend"] == "future-backend"
    assert snapshot["capture_status"] == "unavailable"
    assert snapshot["omitted"] == ["snapshot_id_missing"]
    assert snapshot["span_id"] == "span-future"
    assert step["metadata"]["context_snapshot_id"] == snapshot["snapshot_id"]


def test_event_sequence_sidecar_avoids_rescanning_growing_jsonl(
    tmp_path: Path,
    monkeypatch,
) -> None:
    scans = 0
    original = task_runtime._last_event_sequence

    def counted(path: Path, **kwargs) -> int:
        nonlocal scans
        scans += 1
        return original(path, **kwargs)

    monkeypatch.setattr(task_runtime, "_last_event_sequence", counted)
    recorder = TurnTaskRecorder(
        workspace=_workspace(tmp_path / "ws"),
        session_id="sid-sequence",
        message_id="msg-sequence",
        user_text="sequence",
    )
    for index in range(1000):
        recorder.record_event("activity", {"index": index})

    assert scans <= 1
    sequence_path = recorder.path.parent / task_runtime.EVENT_SEQUENCE_FILENAME
    assert sequence_path.read_text(encoding="ascii") == "1001"
    events = [
        json.loads(line)
        for line in (recorder.path.parent / "events.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    assert [event["sequence"] for event in events] == list(range(1, 1002))


def test_legacy_event_sequence_ignores_pathological_json(tmp_path: Path) -> None:
    task_dir = tmp_path / "workspace" / "tasks" / "task_pathological_legacy"
    task_dir.mkdir(parents=True)
    event_path = task_dir / task_runtime.EVENTS_FILENAME
    event_path.write_text(
        '{"sequence":' + ("9" * 5000) + "}\n",
        encoding="utf-8",
    )
    event_path.chmod(0o600)

    task_runtime._append_task_event(
        task_dir,
        "task_finished",
        {"ok": True},
        workspace_root=tmp_path / "workspace",
    )

    appended = json.loads(event_path.read_text(encoding="utf-8").splitlines()[-1])
    assert appended["sequence"] == 1
    assert appended["event"] == "task_finished"


def test_event_writer_rejects_hardlinks_without_mutating_victims(tmp_path: Path) -> None:
    if not hasattr(os, "link"):
        return
    task_dir = tmp_path / "workspace" / "tasks" / "task_hardlink"
    task_dir.mkdir(parents=True)

    sequence_victim = tmp_path / "sequence-victim"
    sequence_victim.write_text("KEEP-SEQUENCE", encoding="utf-8")
    os.link(sequence_victim, task_dir / task_runtime.EVENT_SEQUENCE_FILENAME)
    try:
        task_runtime._append_task_event(
            task_dir,
            "activity",
            {"value": 1},
            workspace_root=tmp_path / "workspace",
        )
    except OSError:
        pass
    else:
        raise AssertionError("hard-linked sequence state must be rejected")
    assert sequence_victim.read_text(encoding="utf-8") == "KEEP-SEQUENCE"

    (task_dir / task_runtime.EVENT_SEQUENCE_FILENAME).unlink()
    event_victim = tmp_path / "event-victim"
    event_victim.write_text("KEEP-EVENT", encoding="utf-8")
    os.link(event_victim, task_dir / task_runtime.EVENTS_FILENAME)
    try:
        task_runtime._append_task_event(
            task_dir,
            "activity",
            {"value": 2},
            workspace_root=tmp_path / "workspace",
        )
    except OSError:
        pass
    else:
        raise AssertionError("hard-linked event log must be rejected")
    assert event_victim.read_text(encoding="utf-8") == "KEEP-EVENT"


def test_event_writer_rejects_symlinked_task_directory_without_touching_target(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    tasks_dir = workspace / "tasks"
    tasks_dir.mkdir(parents=True)
    external = tmp_path / "external-task"
    external.mkdir(mode=0o755)
    target_mode = stat.S_IMODE(external.stat().st_mode)
    task_dir = tasks_dir / "task_symlink"
    task_dir.symlink_to(external, target_is_directory=True)

    try:
        task_runtime._append_task_event(
            task_dir,
            "activity",
            {"value": 1},
            workspace_root=workspace,
        )
    except OSError:
        pass
    else:
        raise AssertionError("symlinked task directory must be rejected")

    assert stat.S_IMODE(external.stat().st_mode) == target_mode
    assert list(external.iterdir()) == []


def test_task_recorder_rejects_symlinked_tasks_ancestor_before_any_write(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    external = tmp_path / "external-tasks"
    external.mkdir(mode=0o755)
    target_mode = stat.S_IMODE(external.stat().st_mode)
    (workspace_root / task_runtime.TASKS_DIRNAME).symlink_to(
        external,
        target_is_directory=True,
    )
    workspace = Workspace(
        root=workspace_root,
        chat_kind="p2p",
        chat_id="oc_test",
        user_id="ou_test",
        user_name="Alice",
    )

    try:
        TurnTaskRecorder(
            workspace=workspace,
            session_id="sid-ancestor-symlink",
            message_id="msg-ancestor-symlink",
            user_text="must not escape",
        )
    except OSError:
        pass
    else:
        raise AssertionError("symlinked tasks ancestor must be rejected")

    assert stat.S_IMODE(external.stat().st_mode) == target_mode
    assert list(external.iterdir()) == []


def test_event_sequence_rejects_oversized_state_and_recovers_from_log(
    tmp_path: Path,
) -> None:
    task_dir = tmp_path / "workspace" / "tasks" / "task_sequence_width"
    task_dir.mkdir(parents=True)
    sequence_path = task_dir / task_runtime.EVENT_SEQUENCE_FILENAME
    sequence_path.write_text("9" * 64, encoding="ascii")
    sequence_path.chmod(0o600)

    task_runtime._append_task_event(
        task_dir,
        "first",
        {},
        workspace_root=tmp_path / "workspace",
    )
    task_runtime._append_task_event(
        task_dir,
        "second",
        {},
        workspace_root=tmp_path / "workspace",
    )

    events = [
        json.loads(line)
        for line in (task_dir / task_runtime.EVENTS_FILENAME).read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    assert [event["sequence"] for event in events] == [1, 2]
    assert sequence_path.read_text(encoding="ascii") == "2"


def test_event_encoding_failure_does_not_advance_sequence_sidecar(
    tmp_path: Path,
) -> None:
    task_dir = tmp_path / "workspace" / "tasks" / "task_sequence_failure"
    task_dir.mkdir(parents=True)

    try:
        task_runtime._append_task_event(
            task_dir,
            "x" * (70 * 1024),
            {},
            workspace_root=tmp_path / "workspace",
        )
    except ValueError:
        pass
    else:
        raise AssertionError("oversized event type must be rejected")

    sequence_path = task_dir / task_runtime.EVENT_SEQUENCE_FILENAME
    assert not sequence_path.exists()
    task_runtime._append_task_event(
        task_dir,
        "valid",
        {},
        workspace_root=tmp_path / "workspace",
    )
    event = json.loads(
        (task_dir / task_runtime.EVENTS_FILENAME).read_text(encoding="utf-8")
    )
    assert event["sequence"] == 1
    assert sequence_path.read_text(encoding="ascii") == "1"


def test_event_sequence_reconciles_stale_sidecar_with_authoritative_log(
    tmp_path: Path,
) -> None:
    task_dir = tmp_path / "workspace" / "tasks" / "task_stale_sequence"
    task_dir.mkdir(parents=True)
    event_path = task_dir / task_runtime.EVENTS_FILENAME
    event_path.write_text(
        json.dumps({"sequence": 5, "event": "existing"}) + "\n",
        encoding="utf-8",
    )
    event_path.chmod(0o600)
    sequence_path = task_dir / task_runtime.EVENT_SEQUENCE_FILENAME
    sequence_path.write_text("4", encoding="ascii")
    sequence_path.chmod(0o600)

    task_runtime._append_task_event(
        task_dir,
        "next",
        {},
        workspace_root=tmp_path / "workspace",
    )

    events = [json.loads(line) for line in event_path.read_text().splitlines()]
    assert [event["sequence"] for event in events] == [5, 6]
    assert sequence_path.read_text(encoding="ascii") == "6"


def test_event_writer_tightens_legacy_event_log_permissions(tmp_path: Path) -> None:
    if os.name != "posix":
        return
    task_dir = tmp_path / "workspace" / "tasks" / "task_legacy_permissions"
    task_dir.mkdir(parents=True)
    event_path = task_dir / task_runtime.EVENTS_FILENAME
    event_path.write_text(
        json.dumps(
            {
                "event_id": "task_legacy_permissions:1",
                "sequence": 1,
                "event": "task_started",
                "recorded_at": 1.0,
                "data": {},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    event_path.chmod(0o644)

    task_runtime._append_task_event(
        task_dir,
        "task_finished",
        {"ok": True},
        workspace_root=tmp_path / "workspace",
    )

    assert stat.S_IMODE(event_path.stat().st_mode) == 0o600
    events = [json.loads(line) for line in event_path.read_text().splitlines()]
    assert [event["event"] for event in events] == [
        "task_started",
        "task_finished",
    ]
    assert [event["sequence"] for event in events] == [1, 2]


def test_event_writer_rejects_group_writable_legacy_log(tmp_path: Path) -> None:
    if os.name != "posix":
        return
    task_dir = tmp_path / "workspace" / "tasks" / "task_unsafe_permissions"
    task_dir.mkdir(parents=True)
    event_path = task_dir / task_runtime.EVENTS_FILENAME
    original = json.dumps(
        {
            "event_id": "task_unsafe_permissions:1",
            "sequence": 1,
            "event": "task_started",
            "recorded_at": 1.0,
            "data": {},
        }
    ) + "\n"
    event_path.write_text(original, encoding="utf-8")
    event_path.chmod(0o666)

    try:
        task_runtime._append_task_event(
            task_dir,
            "task_finished",
            {"ok": True},
            workspace_root=tmp_path / "workspace",
        )
    except OSError:
        pass
    else:
        raise AssertionError("group-writable legacy event log must be rejected")

    assert event_path.read_text(encoding="utf-8") == original
    assert stat.S_IMODE(event_path.stat().st_mode) == 0o666


def test_large_event_payload_is_manifested_and_remains_tail_visible(tmp_path: Path) -> None:
    recorder = TurnTaskRecorder(
        workspace=_workspace(tmp_path / "ws"),
        session_id="sid-large-event",
        message_id="msg-large-event",
        user_text="large event",
    )
    recorder.tool_started("large_tool", {"content": "x" * (600 * 1024)})

    event_lines = (recorder.path.parent / "events.jsonl").read_bytes().splitlines()
    assert max(len(line) for line in event_lines) < task_runtime.MAX_TASK_EVENT_BYTES
    event = json.loads(event_lines[-1])
    assert event["event"] == "tool_started"
    assert event["data"]["name"] == "large_tool"
    assert event["data"]["payload_truncated"] is True
    assert event["data"]["original_bytes"] > 600 * 1024
    assert event["sanitization"]["payload_truncated"] is True
    assert "x" * 100 not in event_lines[-1].decode("utf-8")


def test_provider_activity_bursts_throttle_task_summary_rewrites(
    tmp_path: Path,
    monkeypatch,
) -> None:
    recorder = TurnTaskRecorder(
        workspace=_workspace(tmp_path / "ws"),
        session_id="sid-activity-burst",
        message_id="msg-activity-burst",
        user_text="activity burst",
    )
    write_calls = 0
    original_write = recorder.write

    def counted_write(**kwargs) -> None:
        nonlocal write_calls
        write_calls += 1
        original_write(**kwargs)

    monkeypatch.setattr(recorder, "write", counted_write)
    for index in range(200):
        span_id = f"command-{index}"
        recorder.span_started(
            f"command {index}",
            "command",
            span_id=span_id,
        )
        recorder.span_finished(
            f"command {index}",
            "command",
            True,
            "completed",
            span_id=span_id,
        )
    recorder.finish(status="succeeded", progress="done")

    assert write_calls < 20
    persisted = json.loads(recorder.path.read_text(encoding="utf-8"))
    assert len(persisted["steps"]) == 200


def test_provider_activity_summary_is_hard_capped_and_explicitly_truncated(
    tmp_path: Path,
) -> None:
    recorder = TurnTaskRecorder(
        workspace=_workspace(tmp_path / "ws"),
        session_id="sid-activity-cap",
        message_id="msg-activity-cap",
        user_text="activity cap",
    )
    for index in range(task_runtime.MAX_PROVIDER_ACTIVITY_SUMMARIES + 100):
        span_id = f"command-cap-{index}"
        recorder.span_started(
            "command",
            "command",
            span_id=span_id,
        )
        recorder.span_finished(
            "command",
            "command",
            True,
            "completed",
            span_id=span_id,
        )
    recorder.finish(status="succeeded", progress="done")

    payload = json.loads(recorder.path.read_text(encoding="utf-8"))
    activity = payload["activity_summary"]
    assert activity == {
        "provider_total": task_runtime.MAX_PROVIDER_ACTIVITY_SUMMARIES + 100,
        "provider_retained": task_runtime.MAX_PROVIDER_ACTIVITY_SUMMARIES,
        "provider_dropped": 100,
        "truncated": True,
    }
    assert len(payload["steps"]) == task_runtime.MAX_PROVIDER_ACTIVITY_SUMMARIES
    assert len(payload["tools"]) == task_runtime.MAX_PROVIDER_ACTIVITY_SUMMARIES
    events = [
        json.loads(line)
        for line in (recorder.path.parent / "events.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    provider_events = [
        event
        for event in events
        if event["event"] in {"span_started", "span_finished"}
        and event["data"].get("kind") == "command"
    ]
    omission_events = [
        event for event in events if event["event"] == "provider_activity_omitted"
    ]
    assert len(provider_events) == task_runtime.MAX_PROVIDER_ACTIVITY_RAW_EVENTS
    assert len(omission_events) == 1


def test_provider_projection_omission_aggregate_preserves_real_counts(
    tmp_path: Path,
) -> None:
    recorder = TurnTaskRecorder(
        workspace=_workspace(tmp_path / "ws"),
        session_id="sid-provider-aggregate",
        message_id="msg-provider-aggregate",
        user_text="provider aggregate",
    )
    recorder.span_started("command", "command", span_id="command-retained")
    recorder.span_finished(
        "command",
        "command",
        True,
        "completed",
        span_id="command-retained",
    )
    recorder.span_started(
        "Provider activity omitted by turn limit",
        "provider_omission",
        span_id="provider-omission",
    )
    recorder.span_finished(
        "Provider activity omitted by turn limit",
        "provider_omission",
        False,
        "Additional provider activity exceeded the per-turn projection limit.",
        span_id="provider-omission",
        data={"omitted_count": 99, "projected_item_limit": 1},
    )
    recorder.finish(status="succeeded", progress="done")

    payload = json.loads(recorder.path.read_text(encoding="utf-8"))
    assert payload["activity_summary"] == {
        "provider_total": 100,
        "provider_retained": 1,
        "provider_dropped": 99,
        "truncated": True,
    }
    assert len(payload["steps"]) == 1
    assert len(payload["tools"]) == 1


def test_provider_projection_omission_aggregate_saturates_at_int64(
    tmp_path: Path,
) -> None:
    maximum = (1 << 63) - 1
    recorder = TurnTaskRecorder(
        workspace=_workspace(tmp_path / "ws"),
        session_id="sid-provider-aggregate-saturated",
        message_id="msg-provider-aggregate-saturated",
        user_text="provider aggregate saturated",
    )
    recorder.span_started("command", "command", span_id="command-retained")
    recorder.span_finished(
        "command",
        "command",
        True,
        "completed",
        span_id="command-retained",
    )
    recorder.span_finished(
        "Provider activity omitted by turn limit",
        "provider_omission",
        False,
        "Additional provider activity exceeded the per-turn projection limit.",
        span_id="provider-omission",
        data={"omitted_count": maximum},
    )

    payload = recorder.to_payload()
    assert payload["activity_summary"] == {
        "provider_total": maximum,
        "provider_retained": 0,
        "provider_dropped": maximum,
        "truncated": True,
    }


def test_raw_observability_text_redacts_auth_uri_and_prefixed_env_secrets() -> None:
    query_value = "".join(("query", "-credential-", "value"))
    credential_uris = (
        "https://" + "user:password@example.com/api",
        "https://" + "userinfo-secret@example.com/repo",
        "postgresql://" + "dbuser:dbpass@db.example/app",
        "redis://" + ":p%40ss@localhost:6379/0",
        "mongodb+srv://" + "mongo:pass@cluster.example/app",
        "ftp://" + "ftpuser:ftppass@example.com/file",
        "https://example.com/?" + "key=" + query_value,
    )
    source = "\n".join(
        (
            "Authorization: Basic dXNlcjpwYXNzd29yZA==",
            "Proxy-Authorization: Basic cHJveHk6cGFzcw==",
            *credential_uris,
            "".join(("OPENAI_", "API_", "KEY", " = ", "abc", " ", "def")),
        )
    )

    result = redact_observability_payload(source)

    serialized = str(result.value)
    assert "dXNlcjpwYXNzd29yZA" not in serialized
    assert "cHJveHk6cGFzcw" not in serialized
    assert "user:password" not in serialized
    assert "userinfo-secret" not in serialized
    assert query_value not in serialized
    for credential in ("dbuser:dbpass", ":p%40ss", "mongo:pass", "ftpuser:ftppass"):
        assert credential not in serialized
    assert "abc def" not in serialized
    assert result.replacement_count >= 10


def test_observability_redaction_handles_cli_cloud_and_escaped_secret_shapes() -> None:
    cli_value = "".join(("cli", "-credential-", "value"))
    cloud_value = "".join(("cloud", "-signature-", "value"))
    session_value = "".join(("cloud", "-session-", "value"))
    escaped_value = "".join(("escaped", "-credential-", "value"))
    source = "\n".join(
        (
            "tool --" + "api-" + "key=" + cli_value + " --safe=true",
            "tool --" + "password " + cli_value + " --mode=read",
            "https://storage.example.test/object?"
            + "X-Goog-"
            + "Signature="
            + cloud_value
            + "&safe=ok#fragment",
            "https://storage.example.test/object?"
            + "X-Amz-"
            + "Security-Token="
            + session_value
            + "&safe=one#fragment",
            "https://storage.example.test/object?"
            + "AWS"
            + "AccessKeyId="
            + cloud_value
            + "&safe=two#fragment",
            "Shared" + "Access" + "Key=" + session_value + ";Endpoint=local",
            'payload={\\"pass' + 'word\\":\\"' + escaped_value + '\\"}',
        )
    )

    result = redact_observability_payload(source)

    serialized = str(result.value)
    assert cli_value not in serialized
    assert cloud_value not in serialized
    assert session_value not in serialized
    assert escaped_value not in serialized
    assert "--safe=true" in serialized
    assert "--mode=read" in serialized
    assert "&safe=ok#fragment" in serialized
    assert "&safe=one#fragment" in serialized
    assert "&safe=two#fragment" in serialized
    assert ";Endpoint=local" in serialized
    assert result.replacement_count == 7

    structured = redact_observability_payload(
        {
            "Account" + "Key": cli_value,
            "signing_" + "key": cli_value,
            "pass" + "phrase": cli_value,
            "Shared" + "Access" + "Key": cli_value,
            "AWS" + "Access" + "Key" + "Id": cli_value,
        }
    )
    assert cli_value not in json.dumps(structured.value)
    assert structured.replacement_count == 5


def test_private_reasoning_omits_redacted_and_thought_provider_blocks() -> None:
    result = omit_private_reasoning_messages(
        [
            {
                "role": "assistant",
                "content": [
                    {"type": "redacted_thinking", "data": "private block"},
                    {
                        "type": "text",
                        "thought": True,
                        "text": "private thought",
                        "thought_signature": "private signature",
                    },
                    {
                        "type": "function_call",
                        "functionCall": {"name": "safe_name"},
                        "thoughtSignature": "private opaque signature",
                    },
                    {"type": "text", "text": "visible answer"},
                ],
            }
        ]
    )

    serialized = json.dumps(result.messages)
    assert "private block" not in serialized
    assert "private thought" not in serialized
    assert "private signature" not in serialized
    assert "private opaque signature" not in serialized
    assert "visible answer" in serialized
    assert result.omission_count == 3


def test_message_omission_helpers_fail_closed_on_deep_payloads() -> None:
    nested: list[object] = []
    cursor = nested
    for _ in range(2000):
        child: list[object] = []
        cursor.append(child)
        cursor = child
    messages = [{"role": "assistant", "content": nested}]

    reasoning = omit_private_reasoning_messages(messages)
    resources = omit_local_resource_paths(messages)

    assert reasoning.messages[0]["content"] == "[TRUNCATED:DEPTH]"
    assert resources.messages[0]["content"] == "[TRUNCATED:DEPTH]"
    assert reasoning.omission_count == 1
    assert resources.omission_count == 1


def test_observability_redaction_and_omission_apply_total_budgets() -> None:
    wide_values = [0] * 200_010

    redaction = redact_observability_payload({"items": wide_values})
    reasoning = omit_private_reasoning_messages(
        [{"role": "assistant", "content": wide_values}]
    )
    resources = omit_local_resource_paths(
        [{"role": "user", "content": wide_values}]
    )

    assert redaction.truncated is True
    assert "item_budget" in redaction.truncation_reasons
    assert "[TRUNCATED:ITEM_BUDGET]" in json.dumps(redaction.value)
    assert reasoning.truncated is True
    assert "item_budget" in reasoning.truncation_reasons
    assert "[TRUNCATED:ITEM_BUDGET]" in json.dumps(reasoning.messages)
    assert resources.truncated is True
    assert "item_budget" in resources.truncation_reasons
    assert "[TRUNCATED:ITEM_BUDGET]" in json.dumps(resources.messages)

    aggregate_strings = redact_observability_payload(
        {"values": ["x" * (1024 * 1024) for _ in range(5)]}
    )
    serialized = json.dumps(aggregate_strings.value)
    assert aggregate_strings.truncated is True
    assert "aggregate_string_budget" in aggregate_strings.truncation_reasons
    assert "[TRUNCATED:STRING_BUDGET]" in serialized
    assert len(serialized) < 4 * 1024 * 1024 + 4096


def test_near_limit_shallow_json_is_rejected_before_json_load(monkeypatch) -> None:
    small = load_bounded_observability_json(b'{"ok":true,"items":[1,2,3]}')
    assert small.ok is True
    assert small.value == {"ok": True, "items": [1, 2, 3]}

    raw = (
        b" " * (7 * 1024 * 1024)
        + b'{"items":['
        + b",".join([b"0"] * 200_010)
        + b"]}"
    )

    def unexpected_json_load(_value):
        raise AssertionError("budget-rejected JSON must not reach json.loads")

    monkeypatch.setattr(observability_redaction.json, "loads", unexpected_json_load)
    loaded = load_bounded_observability_json(raw)

    assert loaded.ok is False
    assert loaded.budget_exhausted is True
    assert loaded.error == "json_item_budget"

    node_heavy = b"[" + b",".join([b"{}"] * 125_010) + b"]"
    loaded_nodes = load_bounded_observability_json(node_heavy)
    assert loaded_nodes.ok is False
    assert loaded_nodes.budget_exhausted is True
    assert loaded_nodes.error == "json_node_budget"


def test_context_snapshot_exposes_observability_budget_truncation(
    tmp_path: Path,
) -> None:
    recorder = TurnTaskRecorder(
        workspace=_workspace(tmp_path / "ws"),
        session_id="sid-budget-context",
        message_id="msg-budget-context",
        user_text="wide context",
    )

    recorder.context_snapshot(
        snapshot_id="ctx_budget",
        backend="native",
        model="test-model",
        iteration=0,
        session_messages=[
            {
                "role": "assistant",
                "content": [0] * 200_010,
            }
        ],
        effective_messages=[],
        tool_schemas=[],
        resources=[],
        coverage="exact_model_input",
        omitted=[],
    )

    artifact = recorder.path.parent / "contexts" / "ctx_budget.json"
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    assert payload["truncated"] is True
    assert payload["coverage"] == "partial"
    assert "observability_budget_exhausted" in payload["omitted"]
    assert payload["sanitization"]["payload_truncated"] is True
    assert payload["sanitization"]["truncation_reasons"]


def test_uri_query_redaction_preserves_safe_query_and_fragment() -> None:
    query_key = "api_" + "key"
    query_value = "".join(("opaque", "-credential-", "value"))
    source = f"https://example.test/path?{query_key}={query_value}&safe=ok#fragment"

    result = redact_observability_payload(source)

    assert result.value == (
        f"https://example.test/path?{query_key}=[REDACTED]&safe=ok#fragment"
    )
    assert result.replacement_count == 1


def test_large_hyphen_text_skips_uri_userinfo_regex(monkeypatch) -> None:
    class UnexpectedUriPattern:
        def subn(self, *_args, **_kwargs):
            raise AssertionError("URI regex must be gated when :// is absent")

    monkeypatch.setattr(
        observability_redaction,
        "_URI_USERINFO",
        UnexpectedUriPattern(),
    )
    source = "a-" * 2_000_000

    result = redact_observability_payload(source)

    assert result.value == source
    assert result.truncated is False


def test_uri_userinfo_redaction_does_not_allow_long_scheme_bypass() -> None:
    scheme = "a" * 128
    result = redact_observability_payload(
        f"{scheme}://worker:private-value@example.test/path"
    )

    assert result.value == f"{scheme}://[REDACTED]@example.test/path"
    assert result.replacement_count == 1


def test_observability_redaction_normalizes_dynamic_keys_and_json_scalars(
    tmp_path: Path,
) -> None:
    dynamic_key = "".join(("sk", "-", "abcdefgh", "ijklmnop"))
    rooted_key = str(tmp_path / "private-key-name")
    result = redact_observability_payload(
        {
            "lookup": {
                dynamic_key: True,
                rooted_key: "value",
            },
            "score": float("inf"),
            "recorded_at": datetime(2026, 8, 18, tzinfo=timezone.utc),
        },
        secrets=(dynamic_key,),
        roots={"workspace": tmp_path},
    )

    serialized = json.dumps(result.value, allow_nan=False)
    assert dynamic_key not in serialized
    assert rooted_key not in serialized
    assert serialized.count("$REDACTED_KEY_") == 2
    assert result.value["score"] is None
    assert result.value["recorded_at"] == "2026-08-18 00:00:00+00:00"
    assert result.replacement_count >= 3


def test_observability_redaction_bounds_pathological_json_depth_and_cycles() -> None:
    oversized_integer_json = '{"count":' + ("9" * 5000) + "}"
    deeply_nested_json = ("[" * 2000) + "0" + ("]" * 2000)
    nested: list[object] = []
    cursor = nested
    for _ in range(2000):
        child: list[object] = []
        cursor.append(child)
        cursor = child
    cyclic: list[object] = []
    cyclic.append(cyclic)

    oversized = redact_observability_payload(oversized_integer_json)
    deep_json = redact_observability_payload(deeply_nested_json)
    deep_python = redact_observability_payload(nested)
    cycle = redact_observability_payload(cyclic)
    oversized_python_integer = redact_observability_payload({"count": 10**5000})

    assert oversized.value == "[TRUNCATED:JSON_LIMIT]"
    assert deep_json.value == "[TRUNCATED:JSON_LIMIT]"
    assert "[TRUNCATED:DEPTH]" in json.dumps(deep_python.value)
    assert cycle.value == ["[TRUNCATED:CYCLE]"]
    assert oversized_python_integer.value == {"count": "[TRUNCATED:INTEGER]"}
    json.dumps(oversized_python_integer.value)
    assert oversized.replacement_count == 1
    assert deep_json.replacement_count == 1
    assert deep_python.replacement_count == 1
    assert cycle.replacement_count == 1
    assert oversized_python_integer.replacement_count == 1


def test_turn_task_records_tool_events_and_job_ids(tmp_path: Path) -> None:
    recorder = TurnTaskRecorder(
        workspace=_workspace(tmp_path / "ws"),
        session_id="sid-2",
        message_id="msg-2",
        user_text="对比两个文件",
    )

    recorder.tool_started(
        "external_diff",
        {"file1": "a.csv", "file2": "b.csv"},
        started_at=100.0,
    )
    recorder.tool_finished(
        "external_diff",
        True,
        "已加入用户队列，任务 ID: job_20260605_120000_deadbeef。",
        finished_at=102.75,
    )
    recorder.finish(status="succeeded", progress="已完成回答。")
    payload = recorder.to_payload()

    assert payload["tools"][0]["name"] == "external_diff"
    assert payload["tools"][0]["status"] == "succeeded"
    assert payload["tools"][0]["elapsed_s"] == 2.8
    assert payload["job_ids"] == ["job_20260605_120000_deadbeef"]


def test_turn_task_records_llm_usage_totals(tmp_path: Path) -> None:
    recorder = TurnTaskRecorder(
        workspace=_workspace(tmp_path / "ws"),
        session_id="sid-3",
        message_id="msg-3",
        user_text="查一下缓存命中",
    )

    recorder.llm_call_finished(
        model="test-model",
        iteration=0,
        finish_reason="tool_calls",
        usage={
            "prompt_tokens": 1000,
            "completion_tokens": 120,
            "total_tokens": 1120,
            "cached_tokens": 250,
            "cache_read_tokens": 250,
            "cache_write_tokens": 0,
        },
        trace_id="trace-1",
        span_id="span-root",
        depth=0,
    )
    recorder.llm_call_finished(
        model="test-model",
        iteration=1,
        finish_reason="stop",
        usage={
            "prompt_tokens": 500,
            "completion_tokens": 80,
            "total_tokens": 580,
            "cached_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 100,
        },
        trace_id="trace-1",
        span_id="span-root",
        depth=0,
    )

    payload = recorder.to_payload()

    assert len(payload["llm_calls"]) == 2
    assert payload["llm_calls"][0]["model"] == "test-model"
    assert payload["usage_totals"]["llm_calls"] == 2
    assert payload["usage_totals"]["prompt_tokens"] == 1500
    assert payload["usage_totals"]["completion_tokens"] == 200
    assert payload["usage_totals"]["total_tokens"] == 1700
    assert payload["usage_totals"]["cached_tokens"] == 250
    assert payload["usage_totals"]["cache_read_tokens"] == 250
    assert payload["usage_totals"]["cache_write_tokens"] == 100
    assert payload["usage_totals"]["cache_hit_calls"] == 1
    assert payload["usage_totals"]["cache_hit_rate"] == 0.1667
    assert payload["usage_totals"]["cache_hit_call_rate"] == 0.5
    recorder.finish(status="succeeded", progress="usage recorded")
    persisted = json.loads(recorder.path.read_text(encoding="utf-8"))
    assert persisted["usage_totals"]["llm_calls"] == 2
    assert persisted["usage_totals"]["cache_hit_calls"] == 1
    assert persisted["usage_totals"]["cache_hit_rate"] == 0.1667
    assert persisted["usage_totals"]["cache_hit_call_rate"] == 0.5


def test_turn_task_persists_llm_backend_on_lifecycle_events_and_summary(
    tmp_path: Path,
) -> None:
    recorder = TurnTaskRecorder(
        workspace=_workspace(tmp_path / "ws"),
        session_id="sid-backend-contract",
        message_id="msg-backend-contract",
        user_text="record backend",
    )
    recorder.llm_call_started(
        model="test-model",
        backend="future-backend",
        iteration=0,
        trace_id="trace-backend",
        span_id="span-backend",
        context_snapshot_id="ctx_backend",
    )
    recorder.llm_call_finished(
        model="test-model",
        backend="future-backend",
        iteration=0,
        finish_reason="stop",
        trace_id="trace-backend",
        span_id="span-backend",
        context_snapshot_id="ctx_backend",
    )
    recorder.finish(status="succeeded", progress="backend recorded")

    persisted = json.loads(recorder.path.read_text(encoding="utf-8"))
    assert persisted["llm_calls"][0]["backend"] == "future-backend"
    events = [
        json.loads(line)
        for line in (recorder.path.parent / task_runtime.EVENTS_FILENAME)
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    lifecycle = {
        event["event"]: event["data"]
        for event in events
        if event["event"] in {"llm_call_started", "llm_call_finished"}
    }
    assert lifecycle["llm_call_started"]["backend"] == "future-backend"
    assert lifecycle["llm_call_finished"]["backend"] == "future-backend"


def test_turn_task_usage_totals_and_inclusive_usage_saturate_at_int64(
    tmp_path: Path,
) -> None:
    maximum = (1 << 63) - 1
    recorder = TurnTaskRecorder(
        workspace=_workspace(tmp_path / "ws"),
        session_id="sid-saturated-usage",
        message_id="msg-saturated-usage",
        user_text="saturated usage",
    )
    recorder.tool_started("delegate", {}, span_id="tool", depth=0)

    for iteration in range(2):
        span_id = f"llm-{iteration}"
        recorder.llm_call_started(
            model="test-model",
            iteration=iteration,
            span_id=span_id,
            parent_span_id="tool",
            depth=1,
        )
        recorder.llm_call_finished(
            model="test-model",
            iteration=iteration,
            span_id=span_id,
            parent_span_id="tool",
            depth=1,
            usage={
                "prompt_tokens": maximum,
                "completion_tokens": maximum,
                "total_tokens": maximum,
                "cached_tokens": maximum,
                "cache_read_tokens": maximum,
                "cache_write_tokens": maximum,
            },
        )

    recorder.tool_finished("delegate", True, "done", span_id="tool", depth=0)
    payload = recorder.to_payload()
    tool_step = next(step for step in payload["steps"] if step["step_id"] == "tool")

    for key in (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "cached_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
    ):
        assert payload["usage_totals"][key] == maximum
        assert tool_step["inclusive_usage"][key] == maximum
    assert payload["usage_totals"]["llm_calls"] == 2


def test_turn_task_usage_rejects_negative_and_unbounded_values(tmp_path: Path) -> None:
    recorder = TurnTaskRecorder(
        workspace=_workspace(tmp_path / "ws"),
        session_id="sid-bounded-usage",
        message_id="msg-bounded-usage",
        user_text="bounded usage",
    )

    recorder.llm_call_finished(
        model="test-model",
        iteration=0,
        usage={
            "prompt_tokens": -10,
            "completion_tokens": 10**1000,
            "total_tokens": float("inf"),
            "cached_tokens": -1,
        },
    )

    payload = recorder.to_payload()
    usage = payload["llm_calls"][0]["usage"]
    assert usage["prompt_tokens"] == 0
    assert usage["completion_tokens"] == 0
    assert usage["total_tokens"] == 0
    assert usage["cached_tokens"] == 0
    assert payload["usage_totals"]["prompt_tokens"] == 0
    assert payload["usage_totals"]["completion_tokens"] == 0

    history_root = tmp_path / "history"
    bad_task = history_root / "user" / "tasks" / "task_bad" / "task.json"
    bad_task.parent.mkdir(parents=True)
    bad_task.write_text(
        '{"schema_version":2,"updated_at":' + ("9" * 5000) + "}",
        encoding="utf-8",
    )
    assert task_runtime.load_task_history(history_root) == []


def test_delegated_task_follows_child_job_terminal_result(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path / "ws")
    recorder = TurnTaskRecorder(
        workspace=workspace,
        session_id="sid-delegated",
        message_id="msg-delegated",
        user_text="修改仓库中的 scripts/foo.py",
    )
    job_id = "job_20260716_210000_deadbeef"
    recorder.record_job_submitted(job_id)
    recorder.finish(
        status="delegated",
        progress="background job submitted",
        final_text="accepted",
        stop_reason="code_route_background",
    )

    delegated = recorder.to_payload()
    assert delegated["status"] == "delegated"
    assert delegated["job_ids"] == [job_id]
    assert delegated["turn_finished_at"] is not None
    assert delegated["finished_at"] is None

    completed = complete_delegated_task(
        workspace,
        task_id=recorder.task_id,
        job_id=job_id,
        result={
            "ok": False,
            "stage": "failed",
            "error_code": "scope_violation",
            "details": {"failed_stage": "validating"},
            "error": "outside scope",
            "outputs": [],
            "finished_at": 123.0,
        },
    )

    assert completed is not None
    assert completed["status"] == "failed"
    assert completed["finished_at"] is not None
    assert completed["job_results"][0]["stage"] == "validating"
    assert completed["job_results"][0]["error_code"] == "scope_violation"


def test_delegated_task_bounds_large_child_result_and_accepts_later_completion(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path / "ws")
    recorder = TurnTaskRecorder(
        workspace=workspace,
        session_id="sid-delegated-bounded",
        message_id="msg-delegated-bounded",
        user_text="run two background jobs",
    )
    first_job = "job_20260716_210000_deadbeef"
    second_job = "job_20260716_210001_feedface"
    recorder.record_job_submitted(first_job)
    recorder.record_job_submitted(second_job)
    recorder.finish(
        # ACP closes a successfully delivered main turn as succeeded; the
        # recorder must project unfinished children as delegated itself.
        status="succeeded",
        progress="answer delivered",
        final_text="accepted",
        stop_reason="background",
    )

    first = complete_delegated_task(
        workspace,
        task_id=recorder.task_id,
        job_id=first_job,
        result={
            "ok": True,
            "summary": "x" * (task_runtime.MAX_TASK_SUMMARY_BYTES + 1024),
            "outputs": ["y" * 4096 for _ in range(50)],
            "finished_at": 123.0,
        },
    )

    assert first is not None
    assert first["status"] == "delegated"
    assert recorder.path.stat().st_size <= task_runtime.MAX_TASK_SUMMARY_BYTES
    persisted_first = json.loads(recorder.path.read_text(encoding="utf-8"))
    assert persisted_first["job_results"][0]["payload_truncated"] is True

    second = complete_delegated_task(
        workspace,
        task_id=recorder.task_id,
        job_id=second_job,
        result={"ok": True, "summary": "done", "finished_at": 124.0},
    )

    assert second is not None
    assert second["status"] == "succeeded"
    assert recorder.path.stat().st_size <= task_runtime.MAX_TASK_SUMMARY_BYTES
    persisted_second = json.loads(recorder.path.read_text(encoding="utf-8"))
    assert len(persisted_second["job_results"]) == 2


def test_concurrent_delegated_completions_preserve_all_results(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = _workspace(tmp_path / "ws")
    recorder = TurnTaskRecorder(
        workspace=workspace,
        session_id="sid-concurrent-completion",
        message_id="msg-concurrent-completion",
        user_text="run two background jobs concurrently",
    )
    job_ids = (
        "job_20260716_210000_deadbeef",
        "job_20260716_210001_feedface",
    )
    for job_id in job_ids:
        recorder.record_job_submitted(job_id)
    recorder.finish(
        status="delegated",
        progress="background jobs submitted",
        final_text="accepted",
        stop_reason="background",
    )

    original_read = task_runtime._read_private_task_json
    original_append = task_runtime._append_task_event
    read_guard = threading.Lock()
    second_task_read = threading.Event()
    task_read_count = 0
    append_guard = threading.Lock()
    terminal_event_persisted = threading.Event()
    completion_event_count = 0

    def delayed_task_read(task_dir: Path, name: str):
        nonlocal task_read_count
        payload = original_read(task_dir, name)
        if name != task_runtime.TASK_FILENAME:
            return payload
        with read_guard:
            task_read_count += 1
            current_read = task_read_count
        if current_read == 1:
            # On the old unlocked implementation the other worker reaches the
            # same read and releases this wait, forcing both to merge the same
            # stale document.  With the completion lock it cannot enter until
            # this worker has committed its result.
            second_task_read.wait(timeout=0.3)
        elif current_read == 2:
            second_task_read.set()
        return payload

    monkeypatch.setattr(task_runtime, "_read_private_task_json", delayed_task_read)

    def delayed_event_append(
        task_dir: Path,
        event_type: str,
        payload: dict,
        *,
        workspace_root: Path,
    ) -> None:
        nonlocal completion_event_count
        if event_type == "job_completed":
            with append_guard:
                completion_event_count += 1
                current_event = completion_event_count
            if current_event == 1:
                # If the completion lock is released before event persistence,
                # the terminal worker can append task_finished first.
                terminal_event_persisted.wait(timeout=0.3)
        original_append(
            task_dir,
            event_type,
            payload,
            workspace_root=workspace_root,
        )
        if event_type == "task_finished":
            terminal_event_persisted.set()

    monkeypatch.setattr(task_runtime, "_append_task_event", delayed_event_append)
    start = threading.Barrier(3)
    results: list[dict] = []
    errors: list[BaseException] = []

    def complete(job_id: str) -> None:
        try:
            start.wait(timeout=5)
            result = complete_delegated_task(
                workspace,
                task_id=recorder.task_id,
                job_id=job_id,
                result={"ok": True, "summary": job_id, "finished_at": 123.0},
            )
            assert result is not None
            results.append(result)
        except BaseException as exc:  # noqa: BLE001 - surface worker assertion failures
            errors.append(exc)

    workers = [threading.Thread(target=complete, args=(job_id,)) for job_id in job_ids]
    for worker in workers:
        worker.start()
    start.wait(timeout=5)
    for worker in workers:
        worker.join(timeout=10)

    assert not any(worker.is_alive() for worker in workers)
    assert errors == []
    assert len(results) == 2
    persisted_task = json.loads(recorder.path.read_text(encoding="utf-8"))
    assert persisted_task["status"] == "succeeded"
    assert {item["job_id"] for item in persisted_task["job_results"]} == set(job_ids)
    persisted_turn = json.loads(
        (recorder.path.parent / task_runtime.TURN_FILENAME).read_text(encoding="utf-8")
    )
    assert persisted_turn["status"] == "succeeded"
    assert {item["job_id"] for item in persisted_turn["job_results"]} == set(job_ids)

    events = [
        json.loads(line)
        for line in (recorder.path.parent / task_runtime.EVENTS_FILENAME)
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    completion_events = [
        event["event"]
        for event in events
        if event["event"] in {"job_completed", "task_finished"}
    ]
    assert completion_events == ["job_completed", "job_completed", "task_finished"]
    completion_lock = recorder.path.parent / task_runtime.COMPLETION_LOCK_FILENAME
    assert completion_lock.stat().st_nlink == 1
    if os.name == "posix":
        assert stat.S_IMODE(completion_lock.stat().st_mode) == 0o600


def test_fast_job_completion_survives_later_recorder_writes_and_finish(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path / "ws")
    recorder = TurnTaskRecorder(
        workspace=workspace,
        session_id="sid-fast-completion",
        message_id="msg-fast-completion",
        user_text="run a fast background job",
    )
    job_id = "job_20260716_210000_deadbeef"

    # The watcher can observe result.json before ToolFinished has returned the
    # accepted job ID to the recorder.  This is the real production ordering
    # that used to let the recorder overwrite the completed child summary.
    completed = complete_delegated_task(
        workspace,
        task_id=recorder.task_id,
        job_id=job_id,
        result={"ok": True, "summary": "done", "finished_at": 123.0},
    )
    assert completed is not None
    assert completed["job_results"][0]["job_id"] == job_id

    recorder.tool_finished(
        "fast_background_tool",
        True,
        f"Background job submitted: {job_id}.",
    )
    recorder.finish(
        status="succeeded",
        progress="answer delivered",
        final_text="accepted",
    )

    persisted_task = json.loads(recorder.path.read_text(encoding="utf-8"))
    persisted_turn = json.loads(
        (recorder.path.parent / task_runtime.TURN_FILENAME).read_text(encoding="utf-8")
    )
    assert persisted_task["status"] == "succeeded"
    assert persisted_task["job_ids"] == [job_id]
    assert persisted_task["job_results"][0]["job_id"] == job_id
    assert persisted_turn["status"] == "succeeded"
    assert persisted_turn["job_results"][0]["job_id"] == job_id
    events = [
        json.loads(line)
        for line in (recorder.path.parent / task_runtime.EVENTS_FILENAME)
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    completion_events = [
        event["event"]
        for event in events
        if event["event"] in {"job_completed", "task_delegated", "task_finished"}
    ]
    assert completion_events == ["job_completed", "task_finished"]


def test_first_fast_completion_cannot_terminalize_before_later_job_registration(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path / "ws")
    recorder = TurnTaskRecorder(
        workspace=workspace,
        session_id="sid-fast-multiple",
        message_id="msg-fast-multiple",
        user_text="run two background jobs in one turn",
    )
    first_job = "job_20260716_210000_deadbeef"
    second_job = "job_20260716_210001_feedface"

    first = complete_delegated_task(
        workspace,
        task_id=recorder.task_id,
        job_id=first_job,
        result={"ok": True, "summary": "first done", "finished_at": 123.0},
    )
    assert first is not None
    assert first["status"] not in {"succeeded", "failed"}

    recorder.tool_finished(
        "first_background_tool",
        True,
        f"Background job submitted: {first_job}.",
    )
    recorder.tool_finished(
        "second_background_tool",
        True,
        f"Background job submitted: {second_job}.",
    )
    recorder.finish(
        status="delegated",
        progress="background jobs submitted",
        final_text="accepted",
        stop_reason="background",
    )

    before_second = json.loads(recorder.path.read_text(encoding="utf-8"))
    assert before_second["status"] == "delegated"
    assert before_second["job_ids"] == [first_job, second_job]
    assert [item["job_id"] for item in before_second["job_results"]] == [first_job]

    completed = complete_delegated_task(
        workspace,
        task_id=recorder.task_id,
        job_id=second_job,
        result={"ok": True, "summary": "second done", "finished_at": 124.0},
    )
    assert completed is not None
    assert completed["status"] == "succeeded"

    persisted_turn = json.loads(
        (recorder.path.parent / task_runtime.TURN_FILENAME).read_text(encoding="utf-8")
    )
    assert persisted_turn["status"] == "succeeded"
    assert {item["job_id"] for item in persisted_turn["job_results"]} == {
        first_job,
        second_job,
    }
    events = [
        json.loads(line)
        for line in (recorder.path.parent / task_runtime.EVENTS_FILENAME)
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    completion_events = [
        event["event"]
        for event in events
        if event["event"] in {"job_completed", "task_delegated", "task_finished"}
    ]
    assert completion_events == [
        "job_completed",
        "task_delegated",
        "job_completed",
        "task_finished",
    ]


def test_main_failure_with_pending_child_stays_pollable_and_cannot_become_success(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path / "ws")
    recorder = TurnTaskRecorder(
        workspace=workspace,
        session_id="sid-main-failure",
        message_id="msg-main-failure",
        user_text="start a job before the main turn fails",
    )
    job_id = "job_20260716_210000_deadbeef"
    recorder.record_job_submitted(job_id)

    recorder.finish(
        status="failed",
        progress="main turn failed",
        error="main model connection failed",
    )

    pending_task = json.loads(recorder.path.read_text(encoding="utf-8"))
    pending_turn = json.loads(
        (recorder.path.parent / task_runtime.TURN_FILENAME).read_text(encoding="utf-8")
    )
    assert pending_task["status"] == "delegated"
    assert pending_task["finished_at"] is None
    assert pending_turn["status"] == "delegated"
    assert pending_turn["main_status"] == "failed"
    assert pending_turn["error"] == "main model connection failed"

    completed = complete_delegated_task(
        workspace,
        task_id=recorder.task_id,
        job_id=job_id,
        result={"ok": True, "summary": "child done", "finished_at": 123.0},
    )

    assert completed is not None
    assert completed["status"] == "failed"
    persisted_turn = json.loads(
        (recorder.path.parent / task_runtime.TURN_FILENAME).read_text(encoding="utf-8")
    )
    assert persisted_turn["status"] == "failed"
    assert persisted_turn["main_status"] == "failed"
    assert persisted_turn["error"] == "main model connection failed"
    assert persisted_turn["job_results"][0]["ok"] is True

    events = [
        json.loads(line)
        for line in (recorder.path.parent / task_runtime.EVENTS_FILENAME)
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    completion_events = [
        event["event"]
        for event in events
        if event["event"] in {"task_delegated", "job_completed", "task_finished"}
    ]
    assert completion_events == ["task_delegated", "job_completed", "task_finished"]


def test_delegated_completion_survives_event_append_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = _workspace(tmp_path / "ws")
    recorder = TurnTaskRecorder(
        workspace=workspace,
        session_id="sid-completion-event-failure",
        message_id="msg-completion-event-failure",
        user_text="run background job",
    )
    job_id = "job_20260716_210000_deadbeef"
    recorder.record_job_submitted(job_id)
    recorder.finish(
        status="delegated",
        progress="background job submitted",
        final_text="accepted",
        stop_reason="background",
    )

    def fail_event_append(*_args, **_kwargs) -> None:
        raise OSError("simulated observability sink failure")

    monkeypatch.setattr(task_runtime, "_append_task_event", fail_event_append)
    completed = complete_delegated_task(
        workspace,
        task_id=recorder.task_id,
        job_id=job_id,
        result={"ok": True, "summary": "done", "finished_at": 123.0},
    )

    assert completed is not None
    assert completed["status"] == "succeeded"
    persisted_task = json.loads(recorder.path.read_text(encoding="utf-8"))
    assert persisted_task["status"] == "succeeded"
    assert persisted_task["job_results"][0]["job_id"] == job_id
    persisted_turn = json.loads(
        (recorder.path.parent / task_runtime.TURN_FILENAME).read_text(encoding="utf-8")
    )
    assert persisted_turn["status"] == "succeeded"
    assert persisted_turn["job_results"][0]["job_id"] == job_id


def test_retried_delegated_completion_repairs_partial_turn_write(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = _workspace(tmp_path / "ws")
    recorder = TurnTaskRecorder(
        workspace=workspace,
        session_id="sid-partial-turn",
        message_id="msg-partial-turn",
        user_text="run background job",
    )
    job_id = "job_20260716_210000_deadbeef"
    recorder.record_job_submitted(job_id)
    recorder.finish(
        status="delegated",
        progress="background job submitted",
        final_text="accepted",
        stop_reason="background",
    )

    original_write = task_runtime._write_private_task_json
    fail_turn_once = True

    def fail_first_completion_turn(
        task_dir: Path,
        name: str,
        payload: dict,
        **kwargs,
    ) -> None:
        nonlocal fail_turn_once
        if name == task_runtime.TURN_FILENAME and payload.get("job_results") and fail_turn_once:
            fail_turn_once = False
            raise OSError("simulated turn write failure")
        original_write(task_dir, name, payload, **kwargs)

    monkeypatch.setattr(
        task_runtime,
        "_write_private_task_json",
        fail_first_completion_turn,
    )
    first = complete_delegated_task(
        workspace,
        task_id=recorder.task_id,
        job_id=job_id,
        result={"ok": True, "summary": "done", "finished_at": 123.0},
    )
    assert first is None
    assert json.loads(recorder.path.read_text(encoding="utf-8"))["status"] == "succeeded"
    partial_turn = json.loads(
        (recorder.path.parent / task_runtime.TURN_FILENAME).read_text(encoding="utf-8")
    )
    assert partial_turn.get("status") != "succeeded"

    retried = complete_delegated_task(
        workspace,
        task_id=recorder.task_id,
        job_id=job_id,
        result={"ok": True, "summary": "done", "finished_at": 123.0},
    )

    assert retried is not None
    assert retried["status"] == "succeeded"
    repaired_turn = json.loads(
        (recorder.path.parent / task_runtime.TURN_FILENAME).read_text(encoding="utf-8")
    )
    assert repaired_turn["status"] == "succeeded"
    assert repaired_turn["job_results"][0]["job_id"] == job_id


def test_v2_nested_steps_aggregate_leaf_llm_usage_once(tmp_path: Path) -> None:
    recorder = TurnTaskRecorder(
        workspace=_workspace(tmp_path / "ws"),
        session_id="sid-nested",
        message_id="msg-nested",
        user_text="nested",
    )
    recorder.tool_started("delegate", {}, span_id="tool", depth=0)
    recorder.span_started(
        "worker",
        "subagent",
        span_id="subagent",
        parent_span_id="tool",
        depth=1,
    )
    recorder.llm_call_started(
        model="test-model",
        iteration=0,
        span_id="llm",
        parent_span_id="subagent",
        depth=2,
        input_estimated_tokens=80,
        context_kind="sliding_window",
    )
    recorder.llm_call_finished(
        model="test-model",
        iteration=0,
        span_id="llm",
        parent_span_id="subagent",
        depth=2,
        usage={
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
            "cached_tokens": 40,
        },
        context_kind="sliding_window",
    )
    recorder.span_finished("worker", "subagent", True, "done", span_id="subagent", depth=1)
    recorder.tool_finished("delegate", True, "done", span_id="tool", depth=0)

    payload = recorder.to_payload()
    by_id = {step["step_id"]: step for step in payload["steps"]}

    assert payload["schema_version"] == 2
    assert payload["usage_totals"]["total_tokens"] == 120
    assert by_id["subagent"]["inclusive_usage"]["total_tokens"] == 120
    assert by_id["tool"]["inclusive_usage"]["total_tokens"] == 120
    assert by_id["tool"]["inclusive_usage"]["cached_tokens"] == 40


def test_subagent_transcript_omits_private_reasoning_before_persistence(
    tmp_path: Path,
) -> None:
    recorder = TurnTaskRecorder(
        workspace=_workspace(tmp_path / "ws"),
        session_id="sid-subagent-reasoning",
        message_id="msg-subagent-reasoning",
        user_text="delegate",
    )
    recorder.span_started("worker", "subagent", span_id="subagent", depth=1)
    recorder.span_finished(
        "worker",
        "subagent",
        True,
        "done",
        span_id="subagent",
        depth=1,
        data={
            "stop_reason": "submit_result",
            "result": {"ok": True},
            "transcript": [
                {"role": "user", "content": "public request"},
                {
                    "role": "assistant",
                    "content": "public answer",
                    "reasoning_content": "provider private reasoning",
                },
            ],
        },
    )

    artifact = recorder.path.parent / "subagents" / "subagent.json"
    text = artifact.read_text(encoding="utf-8")
    payload = json.loads(text)
    assert "provider private reasoning" not in text
    assert "reasoning_content" not in text
    assert payload["transcript"][1]["content"] == "public answer"
    assert payload["sanitization"]["private_reasoning_omission_count"] == 1


def test_failed_llm_call_closes_running_step(tmp_path: Path) -> None:
    recorder = TurnTaskRecorder(
        workspace=_workspace(tmp_path / "ws"),
        session_id="sid-failed",
        message_id="msg-failed",
        user_text="fail",
    )
    recorder.llm_call_started(
        model="test-model",
        iteration=0,
        span_id="llm-failed",
        input_estimated_tokens=100,
        context_kind="sliding_window",
    )
    recorder.span_finished(
        "test-model",
        "llm",
        False,
        "network error",
        span_id="llm-failed",
    )

    step = recorder.to_payload()["steps"][0]
    assert step["status"] == "failed"
    assert step["finished_at"] is not None
    assert step["error"] == "network error"


def test_failed_llm_finish_records_failed_step_and_usage(tmp_path: Path) -> None:
    recorder = TurnTaskRecorder(
        workspace=_workspace(tmp_path / "ws"),
        session_id="sid-failed-finish",
        message_id="msg-failed-finish",
        user_text="fail with usage",
    )
    recorder.llm_call_started(
        model="test-model",
        iteration=0,
        span_id="llm-failed-finish",
        input_estimated_tokens=100,
    )
    recorder.llm_call_finished(
        model="test-model",
        iteration=0,
        span_id="llm-failed-finish",
        finish_reason="failed",
        usage={"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
        ok=False,
    )

    payload = recorder.to_payload()
    step = payload["steps"][0]
    assert step["status"] == "failed"
    assert step["error"] == "failed"
    assert step["actual_usage"]["total_tokens"] == 12
    assert payload["llm_calls"][0]["ok"] is False


def test_ready_task_forecast_is_fixed_after_first_calculation(tmp_path: Path) -> None:
    history_root = tmp_path / "instance"
    for index in range(20):
        task_path = history_root / "history" / "tasks" / f"task_hist_{index}" / "task.json"
        task_path.parent.mkdir(parents=True, exist_ok=True)
        task_path.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "task_id": f"task_hist_{index}",
                    "status": "succeeded",
                    "finished_at": index + 1,
                    "primary_model": "test-model",
                    "context_kind": "sliding_window",
                    "usage_totals": {
                        "prompt_tokens": 100,
                        "completion_tokens": 20,
                        "total_tokens": 120,
                    },
                    "llm_calls": [],
                }
            ),
            encoding="utf-8",
        )
    recorder = TurnTaskRecorder(
        workspace=_workspace(history_root / "live"),
        session_id="sid-fixed",
        message_id="msg-fixed",
        user_text="fixed",
        history_root=history_root,
    )
    recorder.llm_call_started(
        model="test-model",
        iteration=0,
        span_id="llm-1",
        input_estimated_tokens=100,
        context_kind="sliding_window",
    )
    first = recorder.to_payload()["forecast"]

    changed_path = history_root / "history" / "tasks" / "task_hist_new" / "task.json"
    changed_path.parent.mkdir(parents=True, exist_ok=True)
    changed_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "task_id": "task_hist_new",
                "status": "succeeded",
                "finished_at": 999,
                "primary_model": "test-model",
                "context_kind": "sliding_window",
                "usage_totals": {
                    "prompt_tokens": 999999,
                    "completion_tokens": 999999,
                    "total_tokens": 1999998,
                },
                "llm_calls": [],
            }
        ),
        encoding="utf-8",
    )
    recorder.llm_call_started(
        model="test-model",
        iteration=1,
        span_id="llm-2",
        input_estimated_tokens=100,
        context_kind="sliding_window",
    )

    assert first["status"] == "ready"
    assert recorder.to_payload()["forecast"] == first
