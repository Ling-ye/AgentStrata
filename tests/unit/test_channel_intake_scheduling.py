from __future__ import annotations

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from chatcopilot.channels.qq_onebot.config import OneBotChannelConfig
from chatcopilot.channels.qq_onebot.driver import OneBotForwardWebSocketDriver
from chatcopilot.contracts.gateway import ConversationRef
from chatcopilot.gateway.channels import ChannelRuntimeError, ChannelRuntimeManager
from chatcopilot.gateway.state_store import GatewayStateStore
from tests.unit.test_gateway_channels import _Driver, _Ingress, _inbound, ACCOUNT
from tests.unit.test_gateway_channels import _principal
from tests.unit.test_qq_onebot_driver import _FakeConnection, _group_event, _outbound


def event(driver, identity, group="20001"):
    value = _inbound(event_id=identity, body=identity,
                     connection_generation=driver.health().connection_generation)
    return replace(value, evidence=replace(value.evidence,
                   conversation=ConversationRef("group", group)))


def test_busy_turn_does_not_hold_transport_workers_or_acknowledgements(tmp_path):
    async def scenario():
        store = GatewayStateStore(tmp_path / "state")
        ingress = _Ingress()
        ingress.entered, ingress.release = asyncio.Event(), asyncio.Event()
        manager = ChannelRuntimeManager(state_store=store, gateway_ingress=ingress)
        connection = _FakeConnection()
        driver = OneBotForwardWebSocketDriver(
            OneBotChannelConfig("qq", "10001", "ws://127.0.0.1:1", "f" * 32),
            manager.accept_inbound, connection_factory=AsyncMock(return_value=connection))
        manager.register(driver)
        await manager.start()
        await manager.activate()
        try:
            import json
            for index in range(100):
                payload = _group_event(f"message-{index}")
                payload["message_id"] = index + 1
                await connection.incoming.put(json.dumps(payload))
            await asyncio.wait_for(ingress.entered.wait(), timeout=2)
            receipt = await asyncio.wait_for(manager.send(_outbound()), timeout=5)
            assert receipt.stage == "provider_acknowledged"
            for _ in range(200):
                if store.pending_ingress_count() == 100:
                    break
                await asyncio.sleep(.01)
            assert store.pending_ingress_count() == 100
            assert len(ingress.events) == 1
            assert driver.health().state == "ready" and not connection.closed
            ingress.release.set()
            await manager.wait_idle()
            assert len(store.list_ingress(states=("completed",), limit=1000)) == 100
        finally:
            ingress.release.set()
            await manager.stop()
    asyncio.run(scenario())


def test_exact_conversation_queue_preserves_fifo_and_parallelism(tmp_path):
    async def scenario():
        store = GatewayStateStore(tmp_path / "state")
        entered, release, other_done = asyncio.Event(), asyncio.Event(), asyncio.Event()
        ingress = _Ingress()
        async def execute(value, principal):
            ingress.events.append(value)
            if value.evidence.event_id == "z-first":
                entered.set()
                await release.wait()
            if value.evidence.event_id == "other":
                other_done.set()
        ingress.handle_authorized_inbound = execute
        manager = ChannelRuntimeManager(state_store=store, gateway_ingress=ingress, max_concurrent_turns=2,
                                        clock=lambda: 900.0)
        driver = _Driver("main", ACCOUNT, [])
        manager.register(driver)
        await manager.start()
        await manager.activate()
        try:
            await manager.accept_inbound(event(driver, "z-first", "50001"))
            assert not ingress.events
            await entered.wait()
            await manager.accept_inbound(event(driver, "a-second", "50001"))
            await manager.accept_inbound(event(driver, "other", "50016"))
            await asyncio.wait_for(other_done.wait(), timeout=1)
            assert [item.evidence.event_id for item in ingress.events] == ["z-first", "other"]
            release.set()
            await manager.wait_idle()
            assert [item.evidence.event_id for item in ingress.events] == ["z-first", "other", "a-second"]
        finally:
            release.set()
            await manager.stop()
    asyncio.run(scenario())


def test_pending_budget_refuses_new_intake_without_evicting_active_work(tmp_path):
    async def scenario():
        store = GatewayStateStore(tmp_path / "state")
        ingress = _Ingress()
        ingress.entered, ingress.release = asyncio.Event(), asyncio.Event()
        manager = ChannelRuntimeManager(state_store=store, gateway_ingress=ingress, max_pending_ingress=1)
        driver = _Driver("main", ACCOUNT, [])
        manager.register(driver)
        await manager.start()
        await manager.activate()
        first = event(driver, "first")
        try:
            await manager.accept_inbound(first)
            await ingress.entered.wait()
            await manager.accept_inbound(first)
            with pytest.raises(ChannelRuntimeError) as caught:
                await manager.accept_inbound(event(driver, "overflow"))
            assert caught.value.code == "channel_ingress_capacity_exceeded"
            assert store.pending_ingress_count() == 1
            assert store.get_ingress(channel="qq", account_id="10001", event_id="overflow") is None
            assert driver.health().state == "ready"
            ingress.release.set()
            await manager.wait_idle()
        finally:
            ingress.release.set()
            await manager.stop()
    asyncio.run(scenario())


def test_stop_preserves_unstarted_intake_and_restart_replays_it(tmp_path):
    async def scenario():
        store = GatewayStateStore(tmp_path / "state")
        ingress = _Ingress()
        ingress.entered, ingress.release = asyncio.Event(), asyncio.Event()
        manager = ChannelRuntimeManager(state_store=store, gateway_ingress=ingress, max_concurrent_turns=1)
        driver = _Driver("main", ACCOUNT, [])
        manager.register(driver)
        await manager.start()
        await manager.activate()
        await manager.accept_inbound(event(driver, "active"))
        await ingress.entered.wait()
        await manager.accept_inbound(event(driver, "queued"))
        await manager.stop()
        assert store.get_ingress(channel="qq", account_id="10001", event_id="queued").state == "accepted"
        replacement_ingress = _Ingress()
        replacement = ChannelRuntimeManager(state_store=store, gateway_ingress=replacement_ingress)
        replacement.register(_Driver("restart", ACCOUNT, []))
        await replacement.start()
        await replacement.activate()
        try:
            await replacement.wait_idle()
            assert [item.evidence.event_id for item in replacement_ingress.events] == ["queued"]
            assert replacement_ingress.authorization_calls == 0
            assert store.get_ingress(channel="qq", account_id="10001", event_id="queued").state == "completed"
        finally:
            await replacement.stop()
    asyncio.run(scenario())


def test_scheduler_read_error_finishes_waiter_without_replaying(tmp_path, monkeypatch):
    async def scenario():
        store = GatewayStateStore(tmp_path / "state")
        manager = ChannelRuntimeManager(state_store=store, gateway_ingress=_Ingress())
        driver = _Driver("main", ACCOUNT, [])
        manager.register(driver)
        await manager.start()
        await manager.activate()
        original = store.list_ingress
        def failed_read(**kwargs):
            raise OSError("fixture read failure")
        monkeypatch.setattr(store, "list_ingress", failed_read)
        try:
            with pytest.raises(OSError, match="fixture read failure"):
                await asyncio.wait_for(manager.handle_inbound(event(driver, "read-error")), timeout=1)
            assert store.get_ingress(channel="qq", account_id="10001", event_id="read-error").state == "accepted"
        finally:
            monkeypatch.setattr(store, "list_ingress", original)
            await manager.stop()
    asyncio.run(scenario())


def test_large_normalized_intake_reopens_with_full_text(tmp_path):
    from chatcopilot.contracts.gateway import MessageSegment
    from chatcopilot.gateway.state_store import MAX_STATE_JSON_BYTES

    store = GatewayStateStore(tmp_path / "state")
    generation = store.acquire_writer_generation()
    text = "汉" * (256 * 1024)
    value = replace(_inbound(), segments=tuple(MessageSegment(kind="text", text=text) for _ in range(3)))
    assert len(text.encode()) * 3 > MAX_STATE_JSON_BYTES
    store.reserve_ingress(generation=generation, event=value, principal=_principal(value), now=1)
    reopened = GatewayStateStore(tmp_path / "state")
    record = reopened.get_ingress(channel="qq", account_id="10001", event_id="event-1")
    assert record.event.segments == value.segments
    assert len(reopened.list_ingress(states=("accepted",))) == 1


def test_exact_driver_locks_do_not_collide_and_cleanup_cancelled_waiters():
    async def scenario():
        driver = OneBotForwardWebSocketDriver(
            OneBotChannelConfig("qq", "10001", "ws://127.0.0.1:1", "f" * 32), AsyncMock())
        one = event(_Driver("main", ACCOUNT, []), "first", "50001")
        two = event(_Driver("main", ACCOUNT, []), "second", "50016")
        blocked = asyncio.Event()
        async def same_conversation():
            blocked.set()
            async with driver._event_lane(one):
                pytest.fail("same conversation waiter entered while lock held")
        async with driver._event_lane(one):
            waiter = asyncio.create_task(same_conversation())
            await blocked.wait()
            async with driver._event_lane(two):
                assert len(driver._event_lanes) == 2
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
            assert len(driver._event_lanes) == 1
        assert driver._event_lanes == {}
    asyncio.run(scenario())
