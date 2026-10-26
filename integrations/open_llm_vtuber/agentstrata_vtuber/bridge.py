"""Final-response dialogue adapter over the existing authenticated Gateway client."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from hashlib import sha256
import logging
from uuid import uuid4

from chatcopilot.contracts.gateway_rpc import (
    ChatAbortParams, ChatAbortResult, ChatSendParams, ChatSendResult, RunsGetParams, RunsGetResult,
    RunsLatestParams, RunsLatestResult, SessionsCreateParams, SessionsCreateResult, TextRpcSegment,
)
from chatcopilot.protocols.gateway_client import (
    GatewayClientError, GatewayConnectionClosed, GatewayMutationOutcomeUnknown,
    GatewayRecoveryRequired, GatewayWebSocketClient,
)

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class Reply:
    session_id: str
    run_id: str
    text: str
    state: str


@dataclass
class _Turn:
    session_id: str
    params: ChatSendParams | None = None
    key: str | None = None
    run_id: str | None = None
    submitted: bool = False
    cancel_requested: bool = False


class ConversationBridge:
    def __init__(self, client: GatewayWebSocketClient, *, client_id: str):
        self.client = client
        self.client_id = client_id
        self.session_id: str | None = None
        self._turn: _Turn | None = None
        self._lock = asyncio.Lock()
        self._closed = False
        self._closing = False
        self._stop_generation = 0

    def select_history(self, configuration_id: str, history_id: str) -> None:
        if self._lock.locked():
            raise RuntimeError("Stop the current turn before changing history")
        if not configuration_id or not history_id:
            raise ValueError("Select an explicit client history before chatting")
        identity = f"{self.client_id}\0{configuration_id}\0{history_id}"
        selected = "olv_" + sha256(identity.encode()).hexdigest()[:32]
        if self._turn is not None and selected != self.session_id:
            raise RuntimeError("Resolve the pending request before changing history")
        self.session_id = selected

    async def _request(self, method, params, *, key=None, turn: _Turn | None = None):
        for attempt in range(2):
            if self._closed or (self._closing and method in {"sessions.create", "chat.send"}):
                raise RuntimeError("Conversation bridge is closed")
            await self.client.ensure_connected()
            if self._closed:
                await self.client.close()
                raise RuntimeError("Conversation bridge is closed")
            if turn is not None:
                if turn.cancel_requested:
                    if turn.submitted:
                        raise RuntimeError("Gateway acceptance is unknown; cancellation cannot be confirmed")
                    raise asyncio.CancelledError
                turn.submitted = True
            try:
                return await self.client.request(method, params, idempotency_key=key)
            except (GatewayConnectionClosed, GatewayMutationOutcomeUnknown, GatewayRecoveryRequired):
                # Retry only the original mutation/key; a stop forbids resubmitting an unknown input.
                if attempt:
                    raise

    def _clear(self, turn: _Turn) -> None:
        if self._turn is turn:
            self._turn = None

    async def _latest(self, session_id: str) -> RunsLatestResult:
        result = await self._request("runs.latest", RunsLatestParams(session_id))
        if (not isinstance(result, RunsLatestResult)
                or result.run is not None and result.run.session_id != session_id):
            raise RuntimeError("Gateway returned an unexpected run snapshot")
        return result

    async def _snapshot(self, turn: _Turn):
        result = await self._request("runs.get", RunsGetParams(turn.session_id, turn.run_id))
        if (not isinstance(result, RunsGetResult) or result.run.run_id != turn.run_id
                or result.run.session_id != turn.session_id):
            raise RuntimeError("Gateway returned an unexpected run")
        return result.run

    async def chat(self, text: str) -> Reply:
        if not text.strip():
            raise ValueError("Empty transcription does not start an Agent turn")
        if self.session_id is None:
            raise RuntimeError("No client history is selected")
        async with self._lock:
            session_id = self.session_id
            stop_generation = self._stop_generation
            previous = self._turn
            if previous is not None and previous.cancel_requested and previous.run_id is not None:
                if (await self._snapshot(previous)).state in {"completed", "aborted", "failed"}:
                    self._clear(previous)
            if self._stop_generation != stop_generation:
                raise asyncio.CancelledError
            if self._turn is not None:
                raise RuntimeError("A previous request has an unknown outcome; resume or stop it first")
            turn = _Turn(session_id, ChatSendParams(session_id, (TextRpcSegment(text),)), "olv-turn-" + uuid4().hex)
            self._turn = turn
            try:
                created = await self._request("sessions.create", SessionsCreateParams(session_id),
                    key=f"olv-session-{session_id}")
                if not isinstance(created, SessionsCreateResult) or created.session.session_id != session_id:
                    raise RuntimeError("Gateway returned an unexpected session")
                if turn.cancel_requested:
                    raise asyncio.CancelledError
                latest = await self._latest(session_id)
                if latest.run is not None and latest.run.state not in {"completed", "aborted", "failed"}:
                    raise RuntimeError("This history has an unfinished run; resume or stop it before sending")
                await self._submit(turn)
                if turn.cancel_requested:
                    await self._cancel_turn(turn)
                return await self._wait(turn)
            except asyncio.CancelledError:
                try:
                    turn.cancel_requested = True
                    await asyncio.shield(self._cancel_turn(turn))
                except (GatewayClientError, RuntimeError):
                    _LOG.warning("Cancellation unconfirmed; the original input was not resubmitted")
                raise
            finally:
                if not turn.submitted:
                    self._clear(turn)

    async def _submit(self, turn: _Turn) -> None:
        if turn.run_id is not None:
            return
        if turn.params is None or turn.key is None:
            raise RuntimeError("No pending request")
        result = await self._request("chat.send", turn.params, key=turn.key, turn=turn)
        if not isinstance(result, ChatSendResult) or result.session_id != turn.session_id:
            raise RuntimeError("Gateway returned an unexpected accepted run")
        turn.run_id = result.run_id

    async def _wait(self, turn: _Turn) -> Reply:
        while True:
            run = await self._snapshot(turn)
            if run.state in {"completed", "aborted", "failed"}:
                self._clear(turn)
                _LOG.info("dialogue terminal | session=%s run=%s state=%s", turn.session_id, turn.run_id, run.state)
                if run.state == "failed":
                    raise RuntimeError(f"Agent execution failed: {run.error_code}")
                return Reply(turn.session_id, run.run_id,
                    "".join(segment.text for segment in run.segments if isinstance(segment, TextRpcSegment)), run.state)
            if run.state == "recovery_required":
                raise RuntimeError("Agent run requires explicit recovery; it will not be replayed")
            await asyncio.sleep(0.2)

    async def resume(self) -> Reply:
        """Reconcile a pending request or observe the original history's latest run."""
        async with self._lock:
            if self.session_id is None:
                raise RuntimeError("No client history is selected")
            turn = self._turn
            if turn is None:
                turn = _Turn(self.session_id)
                self._turn = turn
                try:
                    latest = await self._latest(self.session_id)
                    if latest.run is None:
                        raise RuntimeError("No previous run to resume")
                    turn.run_id, turn.submitted = latest.run.run_id, True
                finally:
                    if not turn.submitted:
                        self._clear(turn)
            else:
                # Explicit resume reconciles the original input; reconnect must honor stop.
                turn.cancel_requested = False
            await self._submit(turn)
            if turn.cancel_requested:
                await self._cancel_turn(turn)
            return await self._wait(turn)

    async def cancel(self) -> None:
        self._stop_generation += 1
        session_id = self.session_id
        if session_id is None:
            return
        turn = self._turn
        if turn is not None:
            await self._cancel_turn(turn)
            return
        # Bind an idle-history stop before allowing a new chat or history selection.
        async with self._lock:
            latest = await self._latest(session_id)
            if latest.run is not None:
                await self._cancel_turn(_Turn(session_id, run_id=latest.run.run_id, submitted=True))

    async def _cancel_turn(self, turn: _Turn) -> None:
        turn.cancel_requested = True
        if not turn.submitted:
            if turn.params is None:
                raise RuntimeError("Run lookup outcome is unknown; cancellation cannot be confirmed")
            return
        if turn.run_id is None:
            raise RuntimeError("Gateway acceptance is unknown; cancellation cannot be confirmed")
        run = await self._snapshot(turn)
        if run.state in {"completed", "aborted", "failed"}:
            self._clear(turn)
            return
        if run.state not in {"accepted", "running", "abort_requested"}:
            raise RuntimeError("Agent run requires explicit recovery; cancellation cannot be confirmed")
        result = await self._request("chat.abort", ChatAbortParams(turn.session_id, turn.run_id),
            key=f"olv-abort-{turn.run_id}")
        if (not isinstance(result, ChatAbortResult) or result.session_id != turn.session_id
                or result.run_id != turn.run_id):
            raise RuntimeError("Gateway returned an unexpected cancellation receipt")
        if not result.aborted:
            run = await self._snapshot(turn)
            if run.state not in {"completed", "aborted", "failed"}:
                raise RuntimeError("Gateway cancellation was not confirmed")
            self._clear(turn)
        # An accepted abort is not a terminal outcome. The waiter observes the original run.

    async def close(self) -> None:
        if self._closed:
            return
        self._closing = True
        try:
            if self._turn is not None:
                await self.cancel()
        except (GatewayClientError, RuntimeError):
            _LOG.warning("Gateway unavailable while stopping; original run was not replayed")
        finally:
            self._closed = True
            await self.client.close()
