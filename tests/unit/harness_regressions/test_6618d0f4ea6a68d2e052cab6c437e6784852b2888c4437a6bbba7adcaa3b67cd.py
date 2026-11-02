"""Frozen desktop observation recovery checks using the production observer and bridge.

The Gateway client is the external boundary. Its synthetic receipts model distinct
accepted runs, lost acceptance, and terminal model failure without a live service.
"""
from __future__ import annotations

import asyncio

from chatcopilot.contracts.gateway import ChannelAccountRef, ConversationRef
from chatcopilot.contracts.gateway_rpc import (
    ChatSendResult, RunSnapshot, RunsGetResult, RunsLatestResult,
    SessionSnapshot, SessionsCreateResult, TextRpcSegment,
)
from chatcopilot.protocols.gateway_client import GatewayMutationOutcomeUnknown
from integrations.open_llm_vtuber.agentstrata_vtuber.bridge import ConversationBridge
from integrations.open_llm_vtuber.agentstrata_vtuber.perception import DesktopObserver


class Gateway:
    """Only the actual GatewayWebSocketClient methods used by ConversationBridge."""

    def __init__(self, *, first_failed=False, lost_acceptance=False):
        self.calls = []
        self.turns = {}
        self.latest = {}
        self.first_failed = first_failed
        self.lost_acceptance = lost_acceptance
        self.closed = False

    async def ensure_connected(self):
        pass

    async def close(self):
        self.closed = True

    async def request(self, method, params, *, idempotency_key=None):
        self.calls.append((method, idempotency_key))
        if method == "sessions.create":
            return SessionsCreateResult(SessionSnapshot(
                params.session_id, ChannelAccountRef("gateway-client", "avatar-observer"),
                ConversationRef("p2p", params.session_id), "default", False, 0,
            ))
        if method == "runs.latest":
            return RunsLatestResult(self.latest.get(params.session_id))
        if method == "chat.send":
            if idempotency_key not in self.turns:
                number = len(self.turns) + 1
                state = "failed" if self.first_failed and number == 1 else "completed"
                run = RunSnapshot(params.session_id, f"run-{number}", state,
                                  (TextRpcSegment(f"提示 {number}"),),
                                  "synthetic_model_failure" if state == "failed" else None)
                self.turns[idempotency_key] = run
                self.latest[params.session_id] = run
            run = self.turns[idempotency_key]
            if self.lost_acceptance:
                raise GatewayMutationOutcomeUnknown("mutation_outcome_unknown", "receipt lost")
            return ChatSendResult(params.session_id, run.run_id)
        if method == "runs.get":
            return RunsGetResult(self.latest[params.session_id])
        raise AssertionError(f"Unexpected Gateway method: {method}")


def _observer(gateway, now, notices):
    bridge = ConversationBridge(gateway, client_id="avatar-observer")
    observer = DesktopObserver(bridge, clock=lambda: now[0])

    async def present(text, session_id, run_id):
        notices.append((text, session_id, run_id))

    observer.present = present
    return observer


async def _finish(observer):
    assert observer._task is not None
    await asyncio.wait_for(observer._task, 2)


def test_definite_model_failure_retries_same_digest_at_interval():
    async def scenario():
        gateway, now, notices = Gateway(first_failed=True), [0.0], []
        observer = _observer(gateway, now, notices)
        digest = "a" * 64
        assert (await observer.update(True, digest))["state"] == "analyzing"
        await _finish(observer)
        assert observer.last_error == "RuntimeError" and not notices
        now[0] = 29.9
        assert (await observer.update(True, digest))["state"] == "watching"
        assert len(gateway.turns) == 1
        now[0] = 30.0
        assert (await observer.update(True, digest))["state"] == "analyzing"
        await _finish(observer)
        assert len(gateway.turns) == 2
        assert [notice[2] for notice in notices] == ["run-2"]
        assert observer.last_error is None
        now[0] = 90.0
        await observer.update(True, digest)
        assert len(gateway.turns) == 2  # completed state is consumed
        await observer.close()

    asyncio.run(scenario())


def test_definite_failure_coalesces_changes_and_retries_only_latest_screen():
    async def scenario():
        gateway, now, notices = Gateway(first_failed=True), [0.0], []
        observer = _observer(gateway, now, notices)
        await observer.update(True, "1" * 64)
        await _finish(observer)
        now[0] = 10.0
        await observer.update(True, "2" * 64)
        now[0] = 20.0
        await observer.update(True, "3" * 64)
        assert len(gateway.turns) == 1
        now[0] = 30.0
        await observer.update(True, "3" * 64)
        await _finish(observer)
        assert len(gateway.turns) == 2
        assert [notice[2] for notice in notices] == ["run-2"]
        await observer.close()

    asyncio.run(scenario())


def test_unknown_gateway_outcome_recovers_original_turn_before_new_submission():
    async def scenario():
        gateway, now, notices = Gateway(lost_acceptance=True), [0.0], []
        observer = _observer(gateway, now, notices)
        digest = "b" * 64
        await observer.update(True, digest)
        await _finish(observer)
        original_keys = [key for method, key in gateway.calls if method == "chat.send"]
        assert len(original_keys) == 2 and len(set(original_keys)) == 1
        assert len(gateway.turns) == 1 and not notices
        gateway.lost_acceptance = False
        now[0] = 30.0
        await observer.update(True, digest)
        await _finish(observer)
        send_keys = [key for method, key in gateway.calls if method == "chat.send"]
        assert set(send_keys) == {original_keys[0]}
        assert len(gateway.turns) == 1
        assert [notice[2] for notice in notices] == ["run-1"]
        await observer.close()

    asyncio.run(scenario())


def test_recovered_old_screen_result_is_suppressed_and_latest_screen_is_observed():
    async def scenario():
        gateway, now, notices = Gateway(lost_acceptance=True), [0.0], []
        observer = _observer(gateway, now, notices)
        await observer.update(True, "c" * 64)
        await _finish(observer)
        gateway.lost_acceptance = False
        now[0] = 30.0
        await observer.update(True, "d" * 64)
        await _finish(observer)
        assert not notices  # run-1 belongs to the previous sampled screen
        assert len(gateway.turns) == 1
        now[0] = 60.0
        await observer.update(True, "d" * 64)
        await _finish(observer)
        assert [notice[2] for notice in notices] == ["run-2"]
        assert len(gateway.turns) == 2
        await observer.close()

    asyncio.run(scenario())


def test_notice_delivery_failure_does_not_reinvoke_completed_model_turn():
    async def scenario():
        gateway, now, delivered = Gateway(), [0.0], []
        observer = _observer(gateway, now, delivered)

        async def uncertain_delivery(*_args):
            raise ConnectionError("synthetic notice transport closed")

        observer.present = uncertain_delivery
        digest = "e" * 64
        await observer.update(True, digest)
        await _finish(observer)
        assert len(gateway.turns) == 1
        assert observer.last_error == "ConnectionError"
        async def present(*args):
            delivered.append(args)
        observer.present = present
        now[0] = 30.0
        await observer.update(True, digest)
        if observer._task is not None and not observer._task.done():
            await _finish(observer)
        assert len(gateway.turns) == 1 and not delivered
        await observer.close()

    asyncio.run(scenario())
