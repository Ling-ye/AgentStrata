"""Final-response dialogue adapter over the existing authenticated Gateway client."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from hashlib import sha256
import logging
from uuid import uuid4

from chatcopilot.contracts.gateway_rpc import (
    ChatAbortParams, ChatSendParams, ChatSendResult, RunsGetParams, RunsGetResult,
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
    params: ChatSendParams
    key: str
    run_id: str | None = None


class ConversationBridge:
    def __init__(self, client: GatewayWebSocketClient, *, client_id: str):
        self.client = client
        self.client_id = client_id
        self.session_id: str | None = None
        self._turn: _Turn | None = None
        self._lock = asyncio.Lock()

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

    async def _request(self, method, params, *, key=None):
        await self.client.ensure_connected()
        try:
            return await self.client.request(method, params, idempotency_key=key)
        except (GatewayConnectionClosed, GatewayMutationOutcomeUnknown, GatewayRecoveryRequired):
            # The exact mutation and original key are retried, never a replacement input.
            await self.client.ensure_connected()
            return await self.client.request(method, params, idempotency_key=key)

    async def chat(self, text: str) -> Reply:
        if not text.strip():
            raise ValueError("Empty transcription does not start an Agent turn")
        if self.session_id is None:
            raise RuntimeError("No client history is selected")
        async with self._lock:
            session_id = self.session_id
            created = await self._request("sessions.create", SessionsCreateParams(session_id),
                key=f"olv-session-{session_id}")
            if not isinstance(created, SessionsCreateResult) or created.session.session_id != session_id:
                raise RuntimeError("Gateway returned an unexpected session")
            if self._turn is not None:
                raise RuntimeError("A previous request has an unknown outcome; resume or stop it first")
            latest = await self._request("runs.latest", RunsLatestParams(session_id))
            if not isinstance(latest, RunsLatestResult):
                raise RuntimeError("Gateway returned an unexpected run snapshot")
            if latest.run is not None and latest.run.state not in {"completed", "aborted", "failed"}:
                raise RuntimeError("This history has an unfinished run; resume or stop it before sending")
            self._turn = _Turn(ChatSendParams(session_id, (TextRpcSegment(text),)), "olv-turn-" + uuid4().hex)
            try:
                accepted = await self._submit()
                return await self._wait(accepted.run_id, session_id)
            except asyncio.CancelledError:
                try:
                    await asyncio.shield(self.cancel())
                except (GatewayClientError, RuntimeError):
                    _LOG.warning("Cancellation unconfirmed; the original input was not resubmitted")
                raise

    async def _submit(self) -> ChatSendResult:
        turn = self._turn
        if turn is None:
            raise RuntimeError("No pending request")
        result = await self._request("chat.send", turn.params, key=turn.key)
        if not isinstance(result, ChatSendResult) or result.session_id != turn.params.session_id:
            raise RuntimeError("Gateway returned an unexpected accepted run")
        turn.run_id = result.run_id
        return result

    async def _wait(self, run_id: str, session_id: str) -> Reply:
        while True:
            result = await self._request("runs.get", RunsGetParams(session_id, run_id))
            if (not isinstance(result, RunsGetResult) or result.run.run_id != run_id
                    or result.run.session_id != session_id):
                raise RuntimeError("Gateway returned an unexpected run")
            run = result.run
            if run.state in {"completed", "aborted", "failed"}:
                self._turn = None
                _LOG.info("dialogue terminal | session=%s run=%s state=%s", session_id, run_id, run.state)
                if run.state == "failed":
                    raise RuntimeError(f"Agent execution failed: {run.error_code}")
                return Reply(session_id, run_id,
                    "".join(segment.text for segment in run.segments if isinstance(segment, TextRpcSegment)), run.state)
            if run.state == "recovery_required":
                raise RuntimeError("Agent run requires explicit recovery; it will not be replayed")
            await asyncio.sleep(0.2)

    async def resume(self) -> Reply:
        """Reconcile a pending request or observe the original history's latest run."""
        async with self._lock:
            if self.session_id is None:
                raise RuntimeError("No client history is selected")
            if self._turn is not None:
                accepted = await self._submit()
                return await self._wait(accepted.run_id, self._turn.params.session_id)
            latest = await self._request("runs.latest", RunsLatestParams(self.session_id))
            if not isinstance(latest, RunsLatestResult) or latest.run is None:
                raise RuntimeError("No previous run to resume")
            return await self._wait(latest.run.run_id, self.session_id)

    async def cancel(self) -> None:
        session_id = self.session_id
        if session_id is None:
            return
        turn = self._turn
        if turn is not None:
            if turn.run_id is None:
                raise RuntimeError("Gateway acceptance is unknown; cancellation cannot be confirmed")
            run_id = turn.run_id
        else:
            latest = await self._request("runs.latest", RunsLatestParams(session_id))
            run_id = latest.run.run_id if isinstance(latest, RunsLatestResult) and latest.run else None
        if run_id is not None:
            result = await self._request("runs.get", RunsGetParams(session_id, run_id))
            if isinstance(result, RunsGetResult) and result.run.state in {"accepted", "running", "abort_requested"}:
                await self._request("chat.abort", ChatAbortParams(session_id, run_id), key=f"olv-abort-{run_id}")
        self._turn = None

    async def close(self) -> None:
        try:
            if self._turn is not None:
                await self.cancel()
        except (GatewayClientError, RuntimeError):
            _LOG.warning("Gateway unavailable while stopping; original run was not replayed")
        finally:
            await self.client.close()
