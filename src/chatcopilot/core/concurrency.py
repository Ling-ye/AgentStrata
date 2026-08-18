"""Cross-process concurrency limiters for bot runtime hot paths."""
from __future__ import annotations

import os
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional, cast

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

    The acquisition algorithm is intentionally simple: create one unique token
    atomically, sort live tokens, and keep the token only if it is within the
    first N slots. Otherwise remove it and wait. Stale tokens are evicted by TTL
    so crashed workers do not hold capacity forever.
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

    def acquire(self) -> Path:
        start = time.monotonic()
        self.root.mkdir(parents=True, exist_ok=True)

        while True:
            self._cleanup_stale()
            token = self._create_token()
            acquired = False
            try:
                live_tokens = sorted(self.root.glob("*.token"), key=lambda p: p.name)
                if token in live_tokens[: self.max_concurrency]:
                    acquired = True
                    return token
            finally:
                if not acquired and token.exists():
                    try:
                        token.unlink()
                    except OSError:
                        pass

            if self.queue_timeout is not None and time.monotonic() - start >= self.queue_timeout:
                raise TimeoutError(
                    f"{self.name} concurrency queue timed out after {self.queue_timeout:.1f}s"
                )
            time.sleep(self.poll_interval)

    def release(self, token: Path) -> None:
        try:
            token.unlink()
        except FileNotFoundError:
            pass
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
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            os.write(fd, f"pid={os.getpid()}\ncreated={time.time()}\n".encode("ascii"))
        finally:
            os.close(fd)
        return path

    def _cleanup_stale(self) -> None:
        deadline = time.time() - self.stale_seconds
        for token in self.root.glob("*.token"):
            try:
                if token.stat().st_mtime < deadline:
                    token.unlink()
            except FileNotFoundError:
                continue
            except OSError:
                continue


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
