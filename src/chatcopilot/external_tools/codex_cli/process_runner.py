"""Single subprocess execution boundary for Codex CLI invocations."""
from __future__ import annotations

import os
import queue
import signal
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any


ProcessRunner = Callable[..., subprocess.CompletedProcess[str]]
StdoutLineSink = Callable[[str], None]
ProcessPollSink = Callable[[], None]
_DEFAULT_PROCESS_RUNNER = subprocess.run
_CAPTURED_STDOUT_CHARS = 256 * 1024
_CAPTURED_STDERR_CHARS = 64 * 1024
_STREAM_READ_CHARS = 64 * 1024
_STREAM_LINE_CHARS = 1024 * 1024
_CALLBACK_QUEUE_ITEMS = 256
STREAM_LINE_OMISSION_NOTICE = "[stream line omitted: size limit exceeded]"


class _BoundedTextTail:
    """Keep a thread-safe bounded diagnostic tail without retaining the stream."""

    def __init__(self, limit: int) -> None:
        self._limit = max(1, int(limit))
        self._parts: deque[str] = deque()
        self._length = 0
        self._truncated = False
        self._lock = threading.Lock()

    def append(self, value: str) -> None:
        if not value:
            return
        with self._lock:
            if len(value) >= self._limit:
                self._parts.clear()
                self._parts.append(value[-self._limit :])
                self._length = self._limit
                self._truncated = True
                return
            self._parts.append(value)
            self._length += len(value)
            while self._length > self._limit and self._parts:
                excess = self._length - self._limit
                first = self._parts[0]
                if len(first) <= excess:
                    self._parts.popleft()
                    self._length -= len(first)
                else:
                    self._parts[0] = first[excess:]
                    self._length -= excess
                self._truncated = True

    @property
    def truncated(self) -> bool:
        with self._lock:
            return self._truncated

    def render(self) -> str:
        with self._lock:
            value = "".join(self._parts)
            truncated = self._truncated
        if not truncated:
            return value
        marker = "[captured output truncated; showing tail]\n"
        return marker + value[-max(0, self._limit - len(marker)) :]


class _SerializedCallbackDispatcher:
    """Run observer callbacks serially without blocking process supervision."""

    def __init__(
        self,
        *,
        on_stdout_line: StdoutLineSink,
        on_poll: ProcessPollSink | None,
        deadline: float,
    ) -> None:
        self._on_stdout_line = on_stdout_line
        self._on_poll = on_poll
        self._deadline = deadline
        self._queue: queue.Queue[tuple[str, str | None]] = queue.Queue(
            maxsize=_CALLBACK_QUEUE_ITEMS
        )
        self._abort = threading.Event()
        self._producers_done = threading.Event()
        self._poll_pending = threading.Event()
        self._failed = threading.Event()
        self._error_lock = threading.Lock()
        self._error: BaseException | None = None
        self._thread = threading.Thread(
            target=self._run,
            name="codex-callbacks",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def enqueue_stdout_line(self, line: str) -> bool:
        return self._enqueue(("stdout", line), wait=True)

    def enqueue_poll(self, *, required: bool = False) -> bool:
        if self._on_poll is None:
            return True
        while self._poll_pending.is_set():
            if not required:
                return True
            if self._should_stop() or time.monotonic() >= self._deadline:
                return False
            time.sleep(min(0.005, max(0.0, self._deadline - time.monotonic())))
        self._poll_pending.set()
        if self._enqueue(("poll", None), wait=required):
            return True
        self._poll_pending.clear()
        return not required

    def close(self) -> None:
        self._producers_done.set()

    def abort(self) -> None:
        self._abort.set()
        self._producers_done.set()

    def join_until(self, deadline: float) -> bool:
        self._thread.join(timeout=max(0.0, deadline - time.monotonic()))
        return not self._thread.is_alive()

    def raise_if_failed(self) -> None:
        if not self._failed.is_set():
            return
        with self._error_lock:
            error = self._error
        if error is not None:
            raise error

    def _enqueue(self, item: tuple[str, str | None], *, wait: bool) -> bool:
        while not self._should_stop():
            remaining = self._deadline - time.monotonic()
            if remaining <= 0:
                return False
            try:
                if wait:
                    self._queue.put(item, timeout=min(0.05, remaining))
                else:
                    self._queue.put_nowait(item)
                return True
            except queue.Full:
                if not wait:
                    return False
        return False

    def _should_stop(self) -> bool:
        return self._abort.is_set() or self._failed.is_set()

    def _run(self) -> None:
        while True:
            if (
                self._abort.is_set() or self._producers_done.is_set()
            ) and self._queue.empty():
                return
            try:
                kind, payload = self._queue.get(timeout=0.05)
            except queue.Empty:
                continue
            try:
                if self._should_stop():
                    continue
                if kind == "stdout":
                    self._on_stdout_line(payload or "")
                elif self._on_poll is not None:
                    self._on_poll()
            except BaseException as exc:  # noqa: BLE001 - caller-thread re-raise
                with self._error_lock:
                    if self._error is None:
                        self._error = exc
                self._failed.set()
            finally:
                if kind == "poll":
                    self._poll_pending.clear()
                self._queue.task_done()


def run_codex_process(
    command: list[str],
    *,
    cwd: Path,
    prompt: str,
    timeout_seconds: int,
    env: dict[str, str],
    runner: ProcessRunner | None = None,
    on_stdout_line: StdoutLineSink | None = None,
    on_poll: ProcessPollSink | None = None,
) -> subprocess.CompletedProcess[str]:
    """Execute argv without a shell using the repository-wide text contract.

    The default runner streams stdout one bounded line at a time when
    ``on_stdout_line`` is provided and retains only bounded diagnostic tails in
    the returned ``CompletedProcess``.  An injected runner keeps the older
    completion-based contract; its captured stdout is replayed through the
    callback so deterministic callers and tests do not need to emulate
    ``Popen``.
    """

    timeout = max(1, int(timeout_seconds))
    execute = runner or subprocess.run
    if on_stdout_line is None or runner is not None or execute is not _DEFAULT_PROCESS_RUNNER:
        completed = execute(
            command,
            cwd=str(cwd),
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=False,
            env=env,
        )
        if on_poll is not None:
            on_poll()
        if on_stdout_line is not None:
            for line in (completed.stdout or "").splitlines():
                on_stdout_line(line)
        return completed

    process_options: dict[str, Any] = {}
    if os.name == "posix":
        process_options["start_new_session"] = True
    elif os.name == "nt":  # pragma: no cover - requires native Windows validation
        process_options["creationflags"] = int(
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
    process = subprocess.Popen(  # noqa: S603 - argv is built by the confined Codex boundary
        command,
        cwd=str(cwd),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
        env=env,
        **process_options,
    )
    if process.stdin is None or process.stdout is None or process.stderr is None:
        process.kill()
        process.wait()
        raise RuntimeError("Codex subprocess did not expose all requested pipes")
    stdin_pipe = process.stdin
    stdout_pipe = process.stdout
    stderr_pipe = process.stderr

    stdout_tail = _BoundedTextTail(_CAPTURED_STDOUT_CHARS)
    stderr_tail = _BoundedTextTail(_CAPTURED_STDERR_CHARS)
    reader_errors: list[BaseException] = []
    writer_errors: list[BaseException] = []
    stdout_line_truncated = threading.Event()
    deadline = time.monotonic() + timeout
    callbacks = _SerializedCallbackDispatcher(
        on_stdout_line=on_stdout_line,
        on_poll=on_poll,
        deadline=deadline,
    )

    def _read_stdout() -> None:
        line_parts: list[str] = []
        line_length = 0
        line_was_truncated = False
        line_content_limit = _STREAM_LINE_CHARS

        def emit_line() -> None:
            nonlocal line_parts, line_length, line_was_truncated
            line = "".join(line_parts).rstrip("\r\n")
            if line_was_truncated:
                line = STREAM_LINE_OMISSION_NOTICE
                stdout_line_truncated.set()
            callbacks.enqueue_stdout_line(line)
            line_parts = []
            line_length = 0
            line_was_truncated = False

        try:
            while True:
                chunk = stdout_pipe.readline(_STREAM_READ_CHARS)
                if not chunk:
                    break
                stdout_tail.append(chunk)
                if not line_was_truncated:
                    remaining = max(0, line_content_limit - line_length)
                    line_parts.append(chunk[:remaining])
                    line_length += min(len(chunk), remaining)
                    if len(chunk) > remaining:
                        line_was_truncated = True
                if chunk.endswith("\n"):
                    emit_line()
            if line_parts or line_was_truncated:
                emit_line()
        except BaseException as exc:  # noqa: BLE001 - caller-thread re-raise
            reader_errors.append(exc)

    def _read_stderr() -> None:
        try:
            while True:
                chunk = stderr_pipe.read(_STREAM_READ_CHARS)
                if not chunk:
                    break
                stderr_tail.append(chunk)
        except BaseException as exc:  # noqa: BLE001 - caller-thread re-raise
            reader_errors.append(exc)

    def _write_stdin() -> None:
        try:
            stdin_pipe.write(prompt)
            stdin_pipe.flush()
        except BrokenPipeError:
            pass
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller thread
            writer_errors.append(exc)
        finally:
            try:
                stdin_pipe.close()
            except BrokenPipeError:
                pass

    threads = (
        threading.Thread(target=_read_stdout, name="codex-stdout", daemon=True),
        threading.Thread(target=_read_stderr, name="codex-stderr", daemon=True),
        threading.Thread(target=_write_stdin, name="codex-stdin", daemon=True),
    )
    callbacks.start()
    for thread in threads:
        thread.start()

    try:
        while True:
            callbacks.raise_if_failed()
            callbacks.enqueue_poll()
            returncode = process.poll()
            if returncode is not None:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout)
            try:
                returncode = process.wait(timeout=min(0.05, remaining))
            except subprocess.TimeoutExpired:
                continue
            break
        if not callbacks.enqueue_poll(required=True):
            callbacks.raise_if_failed()
            raise subprocess.TimeoutExpired(command, timeout)
        if not _join_threads_until(threads, deadline):
            callbacks.raise_if_failed()
            raise subprocess.TimeoutExpired(command, timeout)
        callbacks.close()
        if not callbacks.join_until(deadline):
            callbacks.raise_if_failed()
            raise subprocess.TimeoutExpired(command, timeout)
        callbacks.raise_if_failed()
        if reader_errors:
            raise reader_errors[0]
        if writer_errors:
            raise writer_errors[0]
    except subprocess.TimeoutExpired as exc:
        callbacks.abort()
        _kill_process_tree(process)
        _wait_after_kill(process)
        cleanup_deadline = time.monotonic() + 1.0
        _join_threads_until(threads, cleanup_deadline)
        callbacks.join_until(cleanup_deadline)
        raise subprocess.TimeoutExpired(
            command,
            timeout,
            output=stdout_tail.render(),
            stderr=stderr_tail.render(),
        ) from exc
    except BaseException:  # noqa: BLE001 - stop the process before caller-thread re-raise
        callbacks.abort()
        _kill_process_tree(process)
        _wait_after_kill(process)
        cleanup_deadline = time.monotonic() + 1.0
        _join_threads_until(threads, cleanup_deadline)
        callbacks.join_until(cleanup_deadline)
        raise
    completed = subprocess.CompletedProcess(
        command,
        returncode,
        stdout_tail.render(),
        stderr_tail.render(),
    )
    completed.stdout_truncated = stdout_tail.truncated  # type: ignore[attr-defined]
    completed.stderr_truncated = stderr_tail.truncated  # type: ignore[attr-defined]
    completed.stdout_line_truncated = stdout_line_truncated.is_set()  # type: ignore[attr-defined]
    return completed


def _join_threads_until(
    threads: tuple[threading.Thread, ...],
    deadline: float,
) -> bool:
    for thread in threads:
        thread.join(timeout=max(0.0, deadline - time.monotonic()))
    return not any(thread.is_alive() for thread in threads)


def _kill_process_tree(process: subprocess.Popen[str]) -> None:
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            process.kill()
        return
    if os.name == "nt":  # pragma: no cover - requires native Windows validation
        try:
            subprocess.run(  # noqa: S603 - PID is obtained from our Popen handle
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                check=False,
                timeout=5,
                shell=False,
            )
        except (OSError, subprocess.SubprocessError):
            process.kill()
        return
    process.kill()


def _wait_after_kill(process: subprocess.Popen[str]) -> None:
    try:
        process.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        process.kill()


__all__ = ["ProcessPollSink", "StdoutLineSink", "run_codex_process"]
