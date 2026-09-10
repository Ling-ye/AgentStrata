"""Cross-process concurrency limiters for bot runtime hot paths."""
from __future__ import annotations

import os
import errno
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO, Iterator, Optional, cast

from chatcopilot.project import ENV_PREFIX, LIMIT_DIRNAME


def _coerce_int(raw: object, fallback: int, *, minimum: int = 1) -> int:
    try:
        value = int(cast(Any, raw))
    except (TypeError, ValueError):
        return fallback
    return max(minimum, value)


def _coerce_float(raw: object, fallback: float, *, minimum: float = 0.1) -> float:
    try:
        value = float(cast(Any, raw))
    except (TypeError, ValueError):
        return fallback
    return max(minimum, value)


def _limit_root() -> Path:
    raw = os.environ.get(f"{ENV_PREFIX}_LIMIT_DIR", "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path(tempfile.gettempdir()) / LIMIT_DIRNAME


class FileTokenLimiter:
    """Small file-token based limiter shared by sibling Python processes.

    Admission is serialized across processes; the admitted token stays locked
    until release. TTL cleanup only removes unlocked, abandoned tokens.
    """

    def __init__(
        self,
        name: str,
        max_concurrency: int,
        *,
        root: Optional[Path] = None,
        queue_timeout: Optional[float] = None,
        stale_seconds: Optional[float] = None,
        poll_interval: float = 0.2,
    ) -> None:
        self.name = name
        self.max_concurrency = max(1, int(max_concurrency))
        self.root = (root or _limit_root()) / name
        self.queue_timeout = queue_timeout
        self.stale_seconds = stale_seconds or _coerce_float(
            os.environ.get(f"{ENV_PREFIX}_LIMIT_STALE_SECONDS"),
            15 * 60.0,
            minimum=5.0,
        )
        self.poll_interval = max(0.05, poll_interval)
        self._held_tokens: dict[Path, tuple[int, BinaryIO]] = {}

    def acquire(self) -> Path:
        start = time.monotonic()
        self.root.mkdir(parents=True, exist_ok=True)

        while True:
            with self._admission_lock(start):
                self._cleanup_stale()
                if sum(1 for _ in self.root.glob("*.token")) < self.max_concurrency:
                    return self._create_token()
            self._wait(start)

    @contextmanager
    def _admission_lock(self, start: float) -> Iterator[None]:
        # Keep this inode stable: unlinking it could create two independent locks.
        fd = os.open(self.root / ".admission.lock", _OPEN_FLAGS | os.O_CREAT, 0o600)
        locked = False
        try:
            while not (locked := _try_lock(fd)):
                self._wait(start)
            yield
        finally:
            try:
                if locked:
                    _unlock(fd)
            finally:
                os.close(fd)

    def _wait(self, start: float) -> None:
        delay = self.poll_interval
        if self.queue_timeout is not None:
            remaining = self.queue_timeout - (time.monotonic() - start)
            if remaining <= 0:
                raise TimeoutError(
                    f"{self.name} concurrency queue timed out after {self.queue_timeout:.1f}s"
                )
            delay = min(delay, remaining)
        time.sleep(delay)

    def release(self, token: Path) -> None:
        held = self._held_tokens.pop(token, None)
        if held is None:
            return
        owner_pid, handle = held
        try:
            if owner_pid == os.getpid():
                _unlock(handle.fileno())
        finally:
            handle.close()
        if owner_pid == os.getpid():
            try:
                token.unlink(missing_ok=True)
            except OSError:
                pass

    @contextmanager
    def slot(self) -> Iterator[None]:
        token = self.acquire()
        try:
            yield
        finally:
            self.release(token)

    def _create_token(self) -> Path:
        token_name = (
            f"{time.monotonic_ns()}-{os.getpid()}-{threading.get_ident()}-"
            f"{uuid.uuid4().hex}.token"
        )
        path = self.root / token_name
        fd = os.open(path, _OPEN_FLAGS | os.O_CREAT | os.O_EXCL, 0o600)
        handle = os.fdopen(fd, "r+b")
        try:
            os.write(fd, f"pid={os.getpid()}\ncreated={time.time()}\n".encode("ascii"))
            if not _try_lock(fd):
                raise RuntimeError("new concurrency token could not be locked")
            self._held_tokens[path] = (os.getpid(), handle)
        except BaseException:
            handle.close()
            path.unlink(missing_ok=True)
            raise
        return path

    def _cleanup_stale(self) -> None:
        deadline = time.time() - self.stale_seconds
        for token in self.root.glob("*.token"):
            try:
                if token.stat().st_mtime < deadline:
                    fd = os.open(token, _OPEN_FLAGS)
                    idle = False
                    try:
                        idle = _try_lock(fd)
                        if idle:
                            _unlock(fd)
                    finally:
                        os.close(fd)
                    # Windows requires closing the handle before unlinking. The
                    # admission lock and unique token names prevent reacquisition.
                    if idle:
                        token.unlink()
            except FileNotFoundError:
                continue
            except OSError:
                continue


_OPEN_FLAGS = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)


def _try_lock(fd: int) -> bool:
    try:
        if os.name == "nt":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            getattr(msvcrt, "locking")(fd, getattr(msvcrt, "LK_NBLCK"), 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as exc:
        if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
            return False
        raise


def _unlock(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        getattr(msvcrt, "locking")(fd, getattr(msvcrt, "LK_UNLCK"), 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


def build_llm_limiter() -> FileTokenLimiter:
    return FileTokenLimiter(
        "llm",
        _coerce_int(os.environ.get(f"{ENV_PREFIX}_LLM_CONCURRENCY"), 5),
        queue_timeout=_optional_timeout(),
    )


def build_heavy_tool_limiter() -> FileTokenLimiter:
    return FileTokenLimiter(
        "heavy_tool",
        _coerce_int(os.environ.get(f"{ENV_PREFIX}_HEAVY_TOOL_CONCURRENCY"), 2),
        queue_timeout=_optional_timeout(),
    )


def _optional_timeout() -> Optional[float]:
    raw = os.environ.get(f"{ENV_PREFIX}_QUEUE_TIMEOUT", "").strip()
    if not raw:
        return None
    return _coerce_float(raw, 300.0)


__all__ = [
    "FileTokenLimiter",
    "build_heavy_tool_limiter",
    "build_llm_limiter",
]
