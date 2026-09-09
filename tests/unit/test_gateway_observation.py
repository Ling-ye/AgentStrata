from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3

import pytest
from fastapi import HTTPException, Response

from chatcopilot.contracts.agent import ContextSnapshotPrepared, LlmCallFinished, ToolStarted
from chatcopilot.contracts.gateway import ChannelAccountRef, ConversationRef, MessageSegment, OutboundEnvelope
from chatcopilot.gateway.observations import RunObserver
from chatcopilot.gateway.observation_runtime import ObservationRecorder
from chatcopilot.gateway import read_model
from chatcopilot.gateway.read_model import gateway_run, gateway_runs
from chatcopilot.gateway.state_store import GatewayStateError, GatewayStateStore
from console.backend.routes import architecture
from console.control.gateway_observability import snapshot
from console.control.instances import BotInstance


def make_run(store: GatewayStateStore, generation: int, suffix: str = "one") -> str:
    session = f"session-{suffix}"
    run = f"run-{suffix}"
    store.create_session(generation=generation, session_id=session,
                         account=ChannelAccountRef("test", "account"),
                         conversation=ConversationRef("p2p", suffix))
    store.begin_run(generation=generation, session_id=session, run_id=run,
                    input_fingerprint=hashlib.sha256(suffix.encode()).hexdigest())
    return run


@pytest.fixture
def state(tmp_path):
    store = GatewayStateStore(tmp_path / "gateway")
    generation = store.acquire_writer_generation()
    run = make_run(store, generation)
    return store, generation, run


def test_reader_is_read_only_and_does_not_advance_generation(state):
    store, generation, run = state
    before = store.database_path.read_bytes()
    names = sorted(path.name for path in store.root.iterdir())
    assert gateway_runs(store.root)["runs"][0]["run_id"] == run
    assert gateway_run(store.root, run)["run"]["state"] == "accepted"
    assert store.get_run(run).generation == generation
    assert store.database_path.read_bytes() == before
    assert sorted(path.name for path in store.root.iterdir()) == names


def test_reader_never_initializes_missing_database(tmp_path):
    root = tmp_path / "missing"
    with pytest.raises(GatewayStateError):
        gateway_runs(root)
    assert not root.exists()


def test_read_only_snapshot_includes_committed_wal_without_touching_source(state):
    store, _, run = state
    with sqlite3.connect(store.database_path) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("INSERT INTO run_observations(run_id,payload_json,created_at) VALUES(?,?,?)",
                       (run, '{"kind":"wal_evidence"}', 10.0))
        writer.commit()
        before = {p.name: p.read_bytes() for p in store.root.iterdir()}
        assert Path(str(store.database_path) + "-wal").exists()
        assert gateway_run(store.root, run)["observations"][0]["kind"] == "wal_evidence"
        assert {p.name: p.read_bytes() for p in store.root.iterdir()} == before


def test_snapshot_rejects_concurrent_write_and_cleans_private_copy(state, monkeypatch, tmp_path):
    store, generation, run = state
    original = read_model._file_identity
    calls = 0
    temporary = tmp_path / "temporary"
    temporary.mkdir(mode=0o700)
    monkeypatch.setattr(read_model.tempfile, "tempdir", str(temporary))
    def race(path):
        nonlocal calls
        calls += 1
        if calls == 4:
            store.append_run_observation(generation=generation, run_id=run, payload={"kind": "concurrent"})
        return original(path)
    monkeypatch.setattr(read_model, "_file_identity", race)
    with pytest.raises(GatewayStateError, match="changed during snapshot"):
        gateway_runs(store.root)
    assert list(temporary.iterdir()) == []


def test_snapshot_rejects_oversized_database(state, monkeypatch):
    store, _, _ = state
    monkeypatch.setattr(read_model, "_MAX_SNAPSHOT_BYTES", 1)
    with pytest.raises(GatewayStateError, match="byte limit"):
        gateway_runs(store.root)


@pytest.mark.parametrize("unsafe", ["root_mode", "file_mode", "hardlink", "root_symlink", "file_symlink", "wal_symlink"])
def test_reader_rejects_unsafe_state(state, tmp_path, unsafe):
    store, _, _ = state
    root = store.root
    if unsafe == "root_mode":
        root.chmod(0o755)
    elif unsafe == "file_mode":
        store.database_path.chmod(0o644)
    elif unsafe == "hardlink":
        os.link(store.database_path, tmp_path / "linked")
    elif unsafe == "root_symlink":
        root = tmp_path / "linked"
        root.symlink_to(store.root, target_is_directory=True)
    elif unsafe == "file_symlink":
        original = store.database_path
        moved = tmp_path / "moved"
        original.rename(moved)
        original.symlink_to(moved)
    elif unsafe == "wal_symlink":
        Path(str(store.database_path) + "-wal").symlink_to(tmp_path / "outside")
    with pytest.raises((GatewayStateError, PermissionError)):
        gateway_runs(root)


def test_observer_keeps_metadata_and_never_stores_arguments_or_context(state, monkeypatch):
    store, generation, run = state
    private = "credential-" + "fixture-value"
    monkeypatch.setenv("TEST_API_KEY", private)
    observer = RunObserver(store, generation, run)
    observer(ToolStarted(name="lookup", arguments={"private_body": private, "reasoning_content": "hidden"}))
    observer(ContextSnapshotPrepared(snapshot_id="ctx-1", backend="native", model="fixture-model", iteration=1,
                                     session_messages=({"content": private},), effective_messages=({"content": "hidden"},)))
    observer(LlmCallFinished(model=private, iteration=1, usage={"total_tokens": 123, "private": private}))
    payload = gateway_run(store.root, run)
    encoded = json.dumps(payload)
    assert private not in encoded
    assert "hidden" not in encoded
    assert "arguments" not in encoded
    assert payload["observations"][-1]["data"]["usage"] == {"total_tokens": 123}
    with sqlite3.connect(store.database_path) as connection:
        raw = str(connection.execute("SELECT payload_json FROM run_observations").fetchall())
    assert private not in raw and "hidden" not in raw


def test_run_evidence_is_scoped_by_run_and_session(state):
    store, generation, run = state
    other = make_run(store, generation, "other")
    for run_id in (run, other):
        RunObserver(store, generation, run_id).record("actor_execution", "application", "agent", "running")
    store.append_event(generation=generation, event="chat.error", payload={
        "run_id": run, "session_id": "session-other", "code": "wrong_session",
    })
    store.append_event(generation=generation, event="chat.error", payload={
        "run_id": run, "session_id": "session-one", "code": "visible_error",
    })
    result = gateway_run(store.root, run)
    assert len(result["observations"]) == 1
    assert [event["data"]["code"] for event in result["events"]] == ["visible_error"]
    assert other not in json.dumps(result)
    assert gateway_run(store.root, "missing") is None
    with pytest.raises(ValueError):
        gateway_run(store.root, "../other")


def test_historical_database_has_no_invented_observations(state):
    store, _, run = state
    with sqlite3.connect(store.database_path) as connection:
        connection.execute("DROP TABLE run_observations")
    before = store.database_path.read_bytes()
    detail = gateway_run(store.root, run)
    assert not detail["observations_available"]
    assert detail["observations"] == []
    assert store.database_path.read_bytes() == before


def test_receipts_preserve_persisted_order_when_timestamps_tie(state):
    store, generation, run = state
    envelope = OutboundEnvelope(
        outbound_id="outbound-order", account=ChannelAccountRef("test", "account"),
        conversation=ConversationRef("p2p", "one"), segments=(MessageSegment(kind="text", text="fixture"),),
        created_at=1.0, session_id="session-one", run_id=run,
    )
    store.enqueue_outbound(generation=generation, envelope=envelope)
    store.begin_outbound_submission(generation=generation, outbound_id=envelope.outbound_id, now=2.0)
    store.acknowledge_outbound(generation=generation, outbound_id=envelope.outbound_id,
                              provider_message_id="fixture-reply", now=3.0)
    with sqlite3.connect(store.database_path) as connection:
        connection.execute("UPDATE delivery_receipts SET receipt_id='z-submitted' WHERE stage='provider_submitted'")
        connection.execute("UPDATE delivery_receipts SET receipt_id='a-acknowledged' WHERE stage='provider_acknowledged'")
    detail = gateway_run(store.root, run)
    assert [receipt["stage"] for receipt in detail["receipts"]] == [
        "gateway_accepted", "provider_submitted", "provider_acknowledged",
    ]
    assert {receipt["outbound_id"] for receipt in detail["receipts"]} == {envelope.outbound_id}


def test_observation_window_and_overflow_marker_are_explicit(state):
    store, generation, run = state
    with sqlite3.connect(store.database_path) as connection:
        connection.executemany("INSERT INTO run_observations(run_id,payload_json,created_at) VALUES(?,?,?)",
                               [(run, '{"kind":"actor_execution"}', float(i)) for i in range(1000)])
    assert not store.append_run_observation(generation=generation, run_id=run, payload={"kind": "late"})
    assert not store.append_run_observation(generation=generation, run_id=run, payload={"kind": "later"})
    detail = gateway_run(store.root, run)
    assert detail["truncated"] and len(detail["observations"]) == 300
    assert detail["observations"][-1]["kind"] == "observations_truncated"


def test_diagnostic_failure_does_not_change_run_authority(state, monkeypatch):
    store, generation, run = state
    def fail(**kwargs):
        raise OSError("write failed")
    monkeypatch.setattr(store, "append_run_observation", fail)
    RunObserver(store, generation, run).record("actor_execution", "application", "agent", "running")
    assert store.get_run(run).state == "accepted"


def test_malformed_record_fails_closed_and_extra_fields_are_not_public(state):
    store, generation, run = state
    store.append_run_observation(generation=generation, run_id=run, payload={
        "kind": "test", "secret_extra": "excluded", "data": {"reasoning_content": "excluded", "arguments": {"x": "excluded"}},
    })
    assert "excluded" not in json.dumps(gateway_run(store.root, run))
    with sqlite3.connect(store.database_path) as connection:
        connection.execute("UPDATE run_observations SET payload_json='broken'")
    with pytest.raises(GatewayStateError):
        gateway_run(store.root, run)


def test_operator_reads_original_private_values_and_api_has_no_store(state, tmp_path, monkeypatch):
    store, generation, run = state
    bot_file = tmp_path / "bot.yaml"
    bot_file.write_text("gateway:\n  state_root_env: FIXTURE_STATE_ROOT\n")
    env_file = tmp_path / "local.env"
    private = "fixture-secret-" + "confidential"
    env_file.write_text(f"FIXTURE_STATE_ROOT={store.root}\nFIXTURE_API_KEY={private}\n")
    env_file.chmod(0o600)
    recorder = ObservationRecorder(store, generation)
    store.start_run(generation=generation, session_id="session-one", run_id=run)
    store.finish_run(generation=generation, session_id="session-one", run_id=run,
                     outcome="completed", result={"final_text": private})
    instance = BotInstance("fixture", str(bot_file), env_file=str(env_file), runtime_kind="gateway")
    indexed = snapshot(instance, run)['run']
    assert recorder.store.body(run, indexed['result_ref'])['payload']['final_text'] == private
    assert private not in json.dumps(gateway_run(store.root, run, secrets=(private,)))
    monkeypatch.setattr(architecture, "get_instance", lambda _: instance)
    response = Response()
    result = architecture.gateway_run_snapshot("fixture", run, response)
    assert result["source"] == "observation_index"
    assert response.headers["Cache-Control"] == "no-store"
    env_file.chmod(0o644)
    with pytest.raises(HTTPException) as error:
        architecture.gateway_snapshot("fixture", Response())
    assert error.value.status_code == 409
    assert error.value.headers["Cache-Control"] == "no-store"
    assert private not in str(error.value.detail)
    assert str(tmp_path) not in str(error.value.detail)


def test_console_does_not_fall_back_to_gateway_state_when_index_is_missing(state, tmp_path, monkeypatch):
    store, _, run = state
    bot_file = tmp_path / "bot.yaml"
    bot_file.write_text("gateway:\n  state_root_env: FIXTURE_STATE_ROOT\n")
    env_file = tmp_path / "local.env"
    env_file.write_text(f"FIXTURE_STATE_ROOT={store.root}\n")
    env_file.chmod(0o600)
    instance = BotInstance("fixture", str(bot_file), env_file=str(env_file), runtime_kind="gateway")
    monkeypatch.setattr(architecture, "get_instance", lambda _: instance)
    def forbidden(*args, **kwargs):
        raise AssertionError("Console must not copy or query the Gateway business database")
    monkeypatch.setattr(read_model, "gateway_run", forbidden)
    monkeypatch.setattr(read_model, "gateway_runs", forbidden)
    with pytest.raises(HTTPException) as error:
        architecture.gateway_run_snapshot("fixture", run, Response())
    assert error.value.status_code == 409
    assert error.value.headers["Cache-Control"] == "no-store"
    assert not (store.root / "observability").exists()
