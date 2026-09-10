from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from chatcopilot.contracts.agent import AgentContentDelta, AgentMessageObserved
from chatcopilot.gateway.observations import RunObserver
from console.backend.routes import architecture
from console.control import observation_stream
from tests.unit.test_observation_workbench import make_run, recorded as _recorded


@pytest.fixture
def recorded(tmp_path):
    return _recorded.__wrapped__(tmp_path)


def test_durable_delta_pages_reconnect_and_late_terminal_events(recorded, monkeypatch):
    state, generation, recorder = recorded
    run_id = make_run(recorded, complete=True)
    observer = RunObserver(state, generation, run_id, agent_stage_span_id="host:actor")
    monkeypatch.setattr(observation_stream, "reader", lambda _: recorder.store)
    instance = SimpleNamespace(runtime_kind="gateway")
    first = AgentContentDelta(text="first", item_id="message", content_kind="message", trace_id=run_id,
        span_id="message", parent_span_id="actor", revision=1)
    observer(first)
    observer(first)
    page = observation_stream.stream_page(instance, run_id, 0)
    frames = [json.loads(frame.split("data: ", 1)[1]) for frame in page["frames"]]
    delta_frames = [frame for frame in frames if frame["observation"]["kind"] == "AgentContentDelta"]
    assert len(delta_frames) == 1 and delta_frames[0]["body"]["payload"]["delta"] == "first"
    assert "first" not in repr(delta_frames[0]["observation"])
    observer(AgentContentDelta(text=" second", item_id="message", content_kind="message", trace_id=run_id,
        span_id="message", parent_span_id="actor", revision=2))
    observer(AgentMessageObserved(text="first second", message_id="message", trace_id=run_id,
        span_id="message", parent_span_id="actor", revision=3))
    tail = observation_stream.stream_page(instance, run_id, page["next_cursor"])
    assert len(tail["frames"]) == 2
    assert observation_stream.stream_page(instance, run_id, tail["next_cursor"])["frames"] == []
    assert observation_stream.stream_page(instance, "run-absent", 0) is None


@pytest.mark.asyncio
async def test_sse_last_event_id_no_store_and_disconnect(monkeypatch):
    cursors = []
    def page(_inst, run_id, after):
        assert run_id == "run-one"
        cursors.append(after)
        return {"frames": ['id: 13\nevent: observation\ndata: {}\n\n'], "next_cursor": 13,
            "has_more": False, "run": {"state": "completed"}}
    monkeypatch.setattr(architecture, "get_instance", lambda _: object())
    monkeypatch.setattr(architecture, "stream_page", page)
    async def disconnected():
        return False
    request = SimpleNamespace(headers={"last-event-id": "12"}, is_disconnected=disconnected)
    response = await architecture.gateway_run_stream("one", "run-one", request, after=4)
    assert response.headers["cache-control"] == "no-store"
    assert "text/event-stream" in response.media_type
    assert await anext(response.body_iterator) == 'id: 13\nevent: observation\ndata: {}\n\n'
    await response.body_iterator.aclose()
    assert cursors == [12]


@pytest.mark.asyncio
@pytest.mark.parametrize("cursor", ["-1", "not-a-number", "9" * 30])
async def test_invalid_sse_cursor_never_reads_storage(cursor, monkeypatch):
    monkeypatch.setattr(architecture, "get_instance", lambda _: object())
    with pytest.raises(HTTPException) as exc:
        await architecture.gateway_run_stream("one", "run-one", SimpleNamespace(headers={"last-event-id": cursor}), after=0)
    assert exc.value.status_code == 400
