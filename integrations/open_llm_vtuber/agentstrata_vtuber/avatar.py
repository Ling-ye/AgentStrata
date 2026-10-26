"""Avatar control is a client-side concern, independent of the Agent runtime."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import json
import logging
from typing import Protocol
from uuid import uuid4
from chatcopilot.core.release_gate import effect_scope, ReleaseBusy

from .config import AdapterConfig

_LOG = logging.getLogger(__name__)


class AvatarController(Protocol):
    def list_actions(self) -> dict: ...
    async def set_expression(self, name: str) -> dict: ...
    async def play_motion(self, name: str) -> dict: ...


class BrowserAvatarController:
    """One explicitly bound browser avatar, with request-specific acknowledgements."""

    def __init__(self, config: AdapterConfig):
        self.config = config
        self._client_id: str | None = None
        self._send: Callable[[str], Awaitable[None]] | None = None
        self._pending: dict[str, asyncio.Future] = {}

    def attach(self, client_id: str, model_name: str, send: Callable[[str], Awaitable[None]]) -> None:
        if model_name != self.config.model_name:
            raise ValueError("Browser model differs from the configured control target")
        if self._client_id is not None and self._client_id != client_id:
            raise ValueError("The configured avatar already has an attached browser")
        self._client_id, self._send = client_id, send

    def detach(self, client_id: str) -> None:
        if client_id != self._client_id:
            return
        self._client_id, self._send = None, None
        for future in tuple(self._pending.values()):
            if not future.done():
                future.set_result({"status": "unknown", "error": "browser_disconnected"})

    def list_actions(self) -> dict:
        return {"model": self.config.model_name, "connected": self._send is not None,
                "expressions": sorted(self.config.expressions), "motions": sorted(self.config.motions)}

    async def set_expression(self, name: str) -> dict:
        if name not in self.config.expressions:
            return {"status": "failed", "error": "unknown_expression"}
        return await self._execute("expression", name, self.config.expressions[name])

    async def play_motion(self, name: str) -> dict:
        motion = self.config.motions.get(name)
        if motion is None:
            return {"status": "failed", "error": "unknown_motion"}
        return await self._execute("motion", name, {"group": motion.group, "index": motion.index})

    async def _execute(self, kind: str, name: str, value: object) -> dict:
        send = self._send
        if send is None:
            return {"status": "unavailable", "error": "browser_not_connected"}
        request_id = uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            with effect_scope():
                await send(json.dumps({"type": "agentstrata-avatar-action", "request_id": request_id,
                    "model_name": self.config.model_name, "kind": kind, "value": value}))
                result = await asyncio.wait_for(future, self.config.action_timeout_seconds)
        except ReleaseBusy:
            result = {"status": "unavailable", "error": "version_switch_not_dispatched"}
        except Exception:  # The browser transport may fail before or after submission.
            result = {"status": "unknown", "error": "action_acknowledgement_missing"}
        finally:
            self._pending.pop(request_id, None)
        _LOG.info("avatar action | request=%s kind=%s name=%s status=%s",
                  request_id, kind, name, result["status"])
        return {"request_id": request_id, **result}

    def acknowledge(self, client_id: str, payload: dict) -> bool:
        if client_id != self._client_id:
            return False
        request_id = payload.get("request_id")
        if not isinstance(request_id, str):
            return False
        future = self._pending.get(request_id)
        status = payload.get("status")
        if future is None or future.done() or status not in {"executed", "failed", "unknown"}:
            return False
        future.set_result({"status": status})
        return True


def record_playback(client_id: str, payload: dict) -> None:
    """Keep client-reported playback evidence in adapter logs, never Gateway run state."""
    status = payload.get("status")
    playback_id = payload.get("playback_id")
    if status in {"started", "completed", "failed", "interrupted", "text_only"} and isinstance(playback_id, str):
        run_id = payload.get("run_id")
        run = run_id[:128] if isinstance(run_id, str) else "unbound"
        _LOG.info("avatar playback | client=%s run=%s playback=%s status=%s",
                  client_id, run, playback_id[:128], status)
