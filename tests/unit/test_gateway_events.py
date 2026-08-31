from __future__ import annotations

import asyncio
from pathlib import Path
import threading

from chatcopilot.application.sessions import SessionManager
from chatcopilot.contracts.gateway import ChannelAccountRef
from chatcopilot.contracts.gateway_rpc import ChannelSnapshot, ChannelStatusEvent
from chatcopilot.gateway.application import GatewaySessionService
from chatcopilot.gateway.events import GatewayEventPublisher
from chatcopilot.gateway.state_store import GatewayStateStore


def test_cross_thread_live_publication_drains_durable_sequence_in_order(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = GatewayStateStore(tmp_path / "state")
        generation = store.acquire_writer_generation()
        sessions = GatewaySessionService(
            state_store=store,
            session_manager=SessionManager(writer_generation=generation),
            generation=generation,
        )
        observed: list[int] = []
        publisher = GatewayEventPublisher(
            state_store=store,
            sessions=sessions,
            generation=generation,
        )
        publisher.attach_live_sink(
            lambda frame: observed.append(frame.seq),
            loop=asyncio.get_running_loop(),
        )

        first_ready = threading.Event()
        release_first = threading.Event()
        original_request = publisher._request_live_drain

        def delayed_request(seq: int) -> None:
            if seq == 1:
                first_ready.set()
                release_first.wait(timeout=1)
            original_request(seq)

        publisher._request_live_drain = delayed_request  # type: ignore[method-assign]
        payload = ChannelStatusEvent(
            ChannelSnapshot(ChannelAccountRef("qq", "10001"), "connected")
        )
        first = asyncio.create_task(
            asyncio.to_thread(publisher.emit, "channel.status", payload)
        )
        assert await asyncio.to_thread(first_ready.wait, 1)
        second = asyncio.create_task(
            asyncio.to_thread(publisher.emit, "channel.status", payload)
        )
        await second
        release_first.set()
        await first
        for _ in range(100):
            if observed == [1, 2]:
                break
            await asyncio.sleep(0)
        assert observed == [1, 2]

    asyncio.run(scenario())
