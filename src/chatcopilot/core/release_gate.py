"""A shared admission lock; deployment holds it while draining and switching."""
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
import os

from chatcopilot.core.private_sqlite import private_lock


class ReleaseBusy(RuntimeError):
    code = "gateway_updating"


@contextmanager
def admission(root: Path):
    try:
        with private_lock(root / "release-admission.lock", exclusive=False):
            yield
    except BlockingIOError as exc:
        raise ReleaseBusy("Gateway is switching versions; no new turn was accepted") from exc


def guarded_ingress(function):
    @wraps(function)
    async def guarded(self, *args, **kwargs):
        with admission(self._state_store.root):
            return await function(self, *args, **kwargs)
    return guarded


def switching(root: Path, timeout: float):
    return private_lock(root / "release-admission.lock", timeout=timeout)


def effects_idle(root: Path, timeout: float):
    return private_lock(root / "release-effects.lock", timeout=timeout)


@contextmanager
def effect_scope():
    root = os.environ.get("AGENTSTRATA_RELEASE_GATE_ROOT")
    if not root:
        yield
        return
    try:
        with private_lock(Path(root) / "release-effects.lock", exclusive=False):
            yield
    except BlockingIOError as exc:
        raise ReleaseBusy("Application is switching versions; the action was not dispatched") from exc
