from __future__ import annotations

import json
import os

import pytest

from chatcopilot.contracts.agent import ContextSnapshotPrepared, LlmCallStarted, LlmCallFinished, ToolStarted, ToolFinished
from chatcopilot.core.trace_archive import TraceArchive, metadata
from chatcopilot.core.trace_capture import TraceCapture, REF_KEY
from chatcopilot.core.trace_transfer import frames, TraceReceiver


def capture() -> TraceCapture:
    value = TraceCapture({"kind": "robot_task", "run_id": "synthetic-run"}, secrets=("synthetic-private-value",))
    value.agent_event(LlmCallStarted(model="synthetic", iteration=0, trace_id="t", span_id="m"))
    value.agent_event(ContextSnapshotPrepared(snapshot_id="ctx", backend="native", model="synthetic", iteration=0,
        trace_id="t", span_id="m", session_messages=({"role": "user", "content": "内容" * 90000},),
        effective_messages=({"role": "user", "content": "内容" * 90000},), tool_schemas=()))
    value.agent_event(LlmCallFinished(model="synthetic", iteration=0, finish_reason="stop", usage={"input_tokens": 3},
                                    ok=True, trace_id="t", span_id="m", visible_response={"content": "response"}))
    value.agent_event(ToolStarted(name="read", arguments={"password": "synthetic-private-value"}, trace_id="t", span_id="tool", parent_span_id="m"))
    value.agent_event(ToolFinished(name="read", ok=True, summary="ok", trace_id="t", span_id="tool", parent_span_id="m",
                                  execution_result={"content": "tool body"}, model_result={"content": "preview"}))
    return value


def test_native_archive_roundtrip_dedup_and_parent(tmp_path):
    store = TraceArchive(tmp_path / "traces")
    recorded = capture()
    reference = store.save(recorded, "completed")
    summary = store.summary(reference["trace_ref"], sha256=reference["sha256"])
    assert summary["sdk_version"] == "4.2.2"
    assert summary["capture_state"] == "available"
    model = next(s for s in summary["spans"] if s["type"] == "llm")
    tool = next(s for s in summary["spans"] if s["type"] == "tool")
    assert tool["parent_id"] == model["id"]
    body = store.step(reference["trace_ref"], tool["id"])
    assert "synthetic-private-value" not in json.dumps(body)
    assert body["output"]["execution_result"]["content"] == "tool body"
    context = next(s for s in summary["spans"] if s["name"] == "ContextSnapshotPrepared")
    resolved = store.step(reference["trace_ref"], context["id"])["output"]
    assert resolved["session_messages"] == resolved["effective_messages"]
    assert resolved["effective_messages"][0]["content"] == "内容" * 90000
    assert len([b for b in recorded.artifacts.values() if len(b) > 500000]) == 1


def test_transfer_and_freeze_survive_source_expiration(tmp_path, monkeypatch):
    recorded = capture()
    receiver = TraceReceiver(recorded.source)
    for frame in frames(recorded, "completed"):
        receiver.receive(frame)
    original = TraceArchive(tmp_path / "original")
    reference = receiver.publish(original)
    frozen = TraceArchive(tmp_path / "frozen")
    frozen_ref = frozen.freeze(original.export(reference["trace_ref"]))
    assert metadata(frozen.load(frozen_ref["trace_ref"]))["expires_at"] is None
    monkeypatch.setattr("chatcopilot.core.trace_archive.time.time", lambda: reference["expires_at"] + 1)
    original.expire(reference["trace_ref"])
    assert original.summary(reference["trace_ref"])["capture_state"] == "expired"
    assert list(frozen.portable_events(frozen_ref["trace_ref"]))
    with pytest.raises(ValueError, match="expired"):
        original.export(reference["trace_ref"])


def test_corrupt_missing_and_cross_source_refs_are_rejected(tmp_path):
    store = TraceArchive(tmp_path / "traces")
    recorded = capture()
    ref = store.save(recorded, "completed")["trace_ref"]
    with pytest.raises(ValueError, match="another execution"):
        store.load(ref, source={"kind": "robot_task", "run_id": "different"})
    with pytest.raises(ValueError, match="reference"):
        store.summary("../../outside")
    digest = next(iter(recorded.artifacts))
    path = store.directory(ref) / "artifacts" / f"{digest}.json"
    path.write_text('"changed"')
    with pytest.raises(ValueError, match="content changed"):
        store.export(ref)
    path.unlink()
    with pytest.raises(FileNotFoundError):
        store.export(ref)


def test_untrusted_reference_looking_body_is_literal(tmp_path):
    recorded = TraceCapture({"kind": "test", "id": "literal"})
    user_data = {REF_KEY: "a" * 64, "bytes": 10}
    recorded.record({"kind": "input"}, user_data)
    store = TraceArchive(tmp_path / "traces")
    ref = store.save(recorded, "completed")["trace_ref"]
    assert list(store.portable_events(ref))[0]["body"] == user_data


def test_incomplete_span_does_not_claim_success(tmp_path):
    recorded = TraceCapture({"kind": "test", "id": "cancel"})
    recorded.agent_event(ToolStarted(name="pending", arguments={}, span_id="s"))
    store = TraceArchive(tmp_path / "traces")
    ref = store.save(recorded, "cancelled")["trace_ref"]
    summary = store.summary(ref)
    assert summary["execution_status"] == "cancelled"
    assert summary["capture_state"] == "partial"
    assert summary["spans"][0]["end_time"] is None


def test_many_events_are_paginated_without_old_source_limit(tmp_path):
    recorded = TraceCapture({"kind": "test", "id": "many"})
    for i in range(2101):
        recorded.record({"kind": "log", "data": {"index": i}}, {"message": str(i)})
    store = TraceArchive(tmp_path / "traces")
    ref = store.save(recorded, "completed")["trace_ref"]
    summary = store.summary(ref, after=2000, limit=200)
    assert summary["span_count"] == 2101
    assert len(summary["spans"]) == 101
    assert not summary["has_more"]


@pytest.mark.parametrize("linked", ["symlink", "hardlink"])
def test_linked_artifact_rejected(tmp_path, linked):
    store = TraceArchive(tmp_path / "traces")
    recorded = capture()
    ref = store.save(recorded, "completed")["trace_ref"]
    path = store.directory(ref) / "artifacts" / f"{next(iter(recorded.artifacts))}.json"
    other = tmp_path / "outside.json"
    if linked == "symlink":
        path.rename(other)
        path.symlink_to(other)
    else:
        os.link(path, other)
    with pytest.raises((ValueError, OSError)):
        store.export(ref)


def test_incomplete_or_reordered_transfer_rejected():
    recorded = capture()
    chunks = list(frames(recorded, "completed"))
    receiver = TraceReceiver(recorded.source)
    with pytest.raises(ValueError, match="Incomplete"):
        receiver.receive(chunks[-1])
    with pytest.raises(ValueError, match="order"):
        receiver.receive(chunks[1])


def test_archive_failure_does_not_publish_partial_index(tmp_path, monkeypatch):
    store = TraceArchive(tmp_path / "traces")
    recorded = capture()
    def fail(*args, **kwargs):
        raise OSError("synthetic disk failure")
    monkeypatch.setattr("chatcopilot.core.trace_archive._write", fail)
    with pytest.raises(OSError):
        store.save(recorded, "completed")
    assert not store.directory(recorded.ref).exists()
    assert not list(store.root.iterdir())


def test_sdk_codec_never_contacts_network_or_loads_dotenv(tmp_path):
    import subprocess
    import sys
    script = '''
import os, socket
def reject(*args, **kwargs):
    raise AssertionError("unexpected network request")
socket.socket.connect = reject
from chatcopilot.core.trace_capture import TraceCapture
value = TraceCapture({"kind": "test", "id": "offline"})
value.record({"kind": "input"}, {"text": "synthetic"})
assert value.finish("completed")["uuid"] == value.ref
assert "DEEPEVAL_DOTENV_PROBE" not in os.environ
print("offline-codec-ok")
'''
    (tmp_path / ".env").write_text("DEEPEVAL_DOTENV_PROBE=unexpected\n")
    from pathlib import Path
    env = {key: value for key, value in os.environ.items() if key in {"PATH", "LANG", "SYSTEMROOT"}}
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
    env["DEEPEVAL_HOME"] = str(tmp_path)
    result = subprocess.run([sys.executable, "-c", script], cwd=tmp_path, env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert "offline-codec-ok" in result.stdout
