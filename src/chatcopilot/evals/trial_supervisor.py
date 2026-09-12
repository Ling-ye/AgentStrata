"""Linux Trial subprocess ownership, bounded IPC and complete process-tree cleanup."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
import math
import multiprocessing
import os
from pathlib import Path
import signal
import sys
import time
from typing import Any, Callable, Iterator, Literal, Mapping

from chatcopilot.evals.models import EvaluationTrial, EvaluationError, TrialExecutionRequest, to_jsonable
from chatcopilot.evals.evaluation_runtime import load_evaluation_runtime
from chatcopilot.evals.result_codec import (ResultContractError, PipelineFailure, error_from_exception,
    _assert_bounded_trial, _MAX_TRIAL_ARTIFACT_BYTES, trial_from_dict as _trial_from_dict, _trial_id)

TrialExecutor = Callable[[TrialExecutionRequest], EvaluationTrial]
CancelCheck = Callable[[], bool]
_MAX_TRIAL_IPC_FRAME_BYTES = _MAX_TRIAL_ARTIFACT_BYTES + 16 * 1024
_TRIAL_STARTUP_TIMEOUT_SECONDS = 15.0
_TRIAL_TERMINATE_GRACE_SECONDS = 5.0
_TRIAL_SUBTREE_TERM_GRACE_SECONDS = 0.5
_TRIAL_SUBTREE_KILL_GRACE_SECONDS = 2.0

class _TrialExecutionCancelled(RuntimeError):
    """The controlling Evaluation cancelled one in-flight Trial."""


class _TrialExecutionDeadlineExceeded(TimeoutError):
    """A hard Trial process deadline expired."""

    def __init__(self, *, scope: Literal["case", "evaluation"], seconds: float) -> None:
        self.scope = scope
        self.seconds = seconds
        super().__init__(f"{scope} execution deadline exceeded after {seconds:.3f} seconds")


class _TrialCleanupFailed(RuntimeError):
    """A Trial supervisor could not prove that every descendant was reaped."""


class _EvaluationDefinitionDrift(RuntimeError):
    """The Suite definition/runtime changed after the parent froze the run."""


@dataclass(frozen=True)
class _TrialExecutionBudget:
    seconds: float
    scope: Literal["case", "evaluation"]


_trial_supervisor_stop_requested = False


def _request_trial_supervisor_stop(_signum: int, _frame: Any) -> None:
    """Ask the dedicated outer supervisor to reap its complete Trial subtree."""

    global _trial_supervisor_stop_requested
    _trial_supervisor_stop_requested = True


def _prepare_trial_process(*, parent_pid: int) -> None:
    """Create a Linux subreaper before any model or tool code runs."""

    global _trial_supervisor_stop_requested
    _trial_supervisor_stop_requested = False
    if not sys.platform.startswith("linux"):
        raise OSError("hard Trial descendant supervision requires Linux/WSL")
    if not Path("/proc/self/task").is_dir():
        raise OSError("hard Trial descendant supervision requires a mounted /proc")
    os.setsid()

    # The outer child never executes Agent or plugin code.  It remains alive as
    # a subreaper while a forked inner child executes the Trial.  A daemonizing
    # or setsid(2) descendant is therefore reparented here rather than to PID 1.
    import ctypes

    signal.signal(signal.SIGTERM, _request_trial_supervisor_stop)
    signal.signal(signal.SIGINT, _request_trial_supervisor_stop)
    libc = ctypes.CDLL(None, use_errno=True)
    pr_set_pdeathsig = 1
    pr_set_child_subreaper = 36
    if libc.prctl(pr_set_pdeathsig, int(signal.SIGTERM), 0, 0, 0) != 0:
        errno_value = ctypes.get_errno()
        raise OSError(errno_value, "could not bind Trial lifetime to Evaluation Core")
    if libc.prctl(pr_set_child_subreaper, 1, 0, 0, 0) != 0:
        errno_value = ctypes.get_errno()
        raise OSError(errno_value, "could not make Trial supervisor a child subreaper")
    if os.getppid() != parent_pid:
        raise RuntimeError("Evaluation Core exited during Trial supervisor startup")


def _encode_trial_ipc_frame(payload: Mapping[str, Any]) -> bytes:
    """Encode one finite canonical JSON control frame with a hard byte limit."""

    try:
        encoded = json.dumps(
            dict(payload),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("Trial IPC frame is not finite canonical JSON") from exc
    if len(encoded) > _MAX_TRIAL_IPC_FRAME_BYTES:
        raise ValueError(f"Trial IPC frame exceeds {_MAX_TRIAL_IPC_FRAME_BYTES} bytes")
    return encoded


def _send_trial_ipc_frame(connection: Any, payload: Mapping[str, Any]) -> None:
    connection.send_bytes(_encode_trial_ipc_frame(payload))


def _reject_trial_ipc_constant(value: str) -> None:
    raise ValueError(f"Trial IPC frame contains non-finite JSON constant {value!r}")


def _trial_ipc_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError(f"Trial IPC frame contains duplicate key {key!r}")
        payload[key] = value
    return payload


def _recv_trial_ipc_frame(connection: Any) -> dict[str, Any]:
    """Receive bounded bytes before parsing; never unpickle child-controlled data."""

    try:
        encoded = connection.recv_bytes(maxlength=_MAX_TRIAL_IPC_FRAME_BYTES)
    except OSError as exc:
        raise ValueError("Trial IPC frame exceeded the receive limit") from exc
    try:
        payload = json.loads(
            encoded.decode("utf-8"),
            parse_constant=_reject_trial_ipc_constant,
            object_pairs_hook=_trial_ipc_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("Trial IPC frame is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("Trial IPC frame must be an object")
    if _encode_trial_ipc_frame(payload) != encoded:
        raise ValueError("Trial IPC frame is not canonical JSON")
    return payload


def _linux_direct_children(pid: int) -> set[int]:
    """Return all process children across the target's Linux thread group."""

    task_root = Path(f"/proc/{pid}/task")
    try:
        task_dirs = tuple(task_root.iterdir())
    except FileNotFoundError:
        return set()
    except OSError as exc:
        raise _TrialCleanupFailed(f"could not inspect Trial process {pid}") from exc
    children: set[int] = set()
    for task_dir in task_dirs:
        try:
            raw = (task_dir / "children").read_text(encoding="ascii").strip()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise _TrialCleanupFailed(
                f"could not inspect Trial process children for {pid}"
            ) from exc
        for value in raw.split():
            if value.isdigit():
                children.add(int(value))
    return children


def _linux_trial_descendants() -> tuple[int, ...]:
    """Enumerate the dedicated supervisor's complete current descendant tree."""

    pending = list(_linux_direct_children(os.getpid()))
    seen: set[int] = set()
    ordered: list[int] = []
    while pending:
        pid = pending.pop()
        if pid in seen:
            continue
        seen.add(pid)
        ordered.append(pid)
        pending.extend(_linux_direct_children(pid) - seen)
    return tuple(ordered)


def _reap_trial_children() -> None:
    """Reap every exited child adopted by the dedicated Trial subreaper."""

    while True:
        try:
            pid, _status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        except InterruptedError:
            continue
        if pid == 0:
            return


def _signal_trial_descendants(signum: int) -> None:
    # Children are signalled before their parents.  The loop in the caller
    # repeats discovery, so descendants forked during shutdown are included.
    for pid in reversed(_linux_trial_descendants()):
        try:
            os.kill(pid, signum)
        except ProcessLookupError:
            continue
        except OSError as exc:
            raise _TrialCleanupFailed(f"could not signal Trial descendant {pid}") from exc


def _wait_for_empty_trial_subtree(*, signum: int, grace_seconds: float) -> bool:
    deadline = time.monotonic() + grace_seconds
    while True:
        _signal_trial_descendants(signum)
        _reap_trial_children()
        if not _linux_trial_descendants():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)


def _cleanup_trial_subtree() -> None:
    """Terminate and reap even daemonized/session-escaped Trial descendants."""

    if _wait_for_empty_trial_subtree(
        signum=signal.SIGTERM,
        grace_seconds=_TRIAL_SUBTREE_TERM_GRACE_SECONDS,
    ):
        return
    if _wait_for_empty_trial_subtree(
        signum=signal.SIGKILL,
        grace_seconds=_TRIAL_SUBTREE_KILL_GRACE_SECONDS,
    ):
        return
    remaining = _linux_trial_descendants()
    raise _TrialCleanupFailed(
        "Trial descendants remained after SIGKILL: " + ",".join(str(pid) for pid in remaining)
    )


def _execute_trial_in_fork(
    sender: Any,
    outer_sender: Any,
    request: TrialExecutionRequest,
    executor: TrialExecutor,
) -> None:
    """Run Agent/plugin code in the inner child and emit canonical JSON only."""

    outer_sender.close()
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    try:
        os.setsid()
        from chatcopilot.evals.trial_capture import capture

        phase = "execution"
        with capture(lambda observation: _send_trial_ipc_frame(sender, {"kind": "observation", "execution": observation})):
            trial = executor(request)
        phase = "result_validation"
        if not isinstance(trial, EvaluationTrial):
            raise ResultContractError("executor: expected EvaluationTrial")
        _assert_bounded_trial(trial)
        trial_payload = to_jsonable(trial)
        encoded_trial = json.dumps(
            trial_payload,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(encoded_trial) > _MAX_TRIAL_ARTIFACT_BYTES:
            raise ValueError(f"Trial exceeds {_MAX_TRIAL_ARTIFACT_BYTES} IPC bytes")
        _send_trial_ipc_frame(sender, {"kind": "result", "trial": trial_payload})
    except BaseException as exc:  # noqa: BLE001 - isolated execution boundary
        try:
            _send_trial_ipc_frame(
                sender,
                {
                    "kind": (
                        "definition_drift"
                        if isinstance(exc, _EvaluationDefinitionDrift)
                        else "error"
                    ),
                    "error_type": type(exc).__name__,
                    "message": str(exc)[:4096],
                    "failure": to_jsonable(error_from_exception(exc, phase)),
                },
            )
        except (BrokenPipeError, EOFError, OSError, ValueError):
            pass
    finally:
        sender.close()


def _await_inner_trial_frame(receiver: Any, executor_pid: int, outer_sender: Any = None) -> dict[str, Any] | None:
    """Wait for inner evidence while remaining responsive to parent death."""

    while not _trial_supervisor_stop_requested:
        try:
            if receiver.poll(0.05):
                frame = _recv_trial_ipc_frame(receiver)
                if frame.get("kind") == "observation" and outer_sender is not None:
                    _send_trial_ipc_frame(outer_sender, frame)
                    continue
                return frame
        except (EOFError, OSError, ValueError) as exc:
            return {
                "kind": "error",
                "error_type": type(exc).__name__,
                "message": str(exc)[:4096],
            }
        try:
            waited, status = os.waitpid(executor_pid, os.WNOHANG)
        except ChildProcessError:
            waited, status = executor_pid, 0
        if waited == executor_pid:
            try:
                if receiver.poll(0.05):
                    return _recv_trial_ipc_frame(receiver)
            except (EOFError, OSError, ValueError) as exc:
                return {
                    "kind": "error",
                    "error_type": type(exc).__name__,
                    "message": str(exc)[:4096],
                }
            return {
                "kind": "error",
                "error_type": "RuntimeError",
                "message": f"Trial executor exited without evidence (status={status})",
            }
    return None


def _trial_process_main(
    sender: Any,
    request: TrialExecutionRequest,
    executor: TrialExecutor,
    parent_pid: int,
) -> None:
    """Outer child: supervise, reap, then forward one bounded JSON frame."""

    inner_receiver: Any | None = None
    inner_sender: Any | None = None
    executor_pid: int | None = None
    cleanup_attempted = False
    try:
        try:
            _prepare_trial_process(parent_pid=parent_pid)
        except BaseException as exc:  # noqa: BLE001 - child startup boundary
            _send_trial_ipc_frame(
                sender,
                {
                    "kind": "startup_error",
                    "error_type": type(exc).__name__,
                    "message": str(exc)[:4096],
                },
            )
            return
        _send_trial_ipc_frame(sender, {"kind": "ready", "pid": os.getpid()})
        if _trial_supervisor_stop_requested:
            return

        try:
            inner_receiver, inner_sender = multiprocessing.get_context("fork").Pipe(duplex=False)
            executor_pid = os.fork()
        except OSError as exc:
            _send_trial_ipc_frame(
                sender,
                {
                    "kind": "error",
                    "error_type": type(exc).__name__,
                    "message": str(exc)[:4096],
                },
            )
            return
        if executor_pid == 0:
            inner_receiver.close()
            try:
                _execute_trial_in_fork(inner_sender, sender, request, executor)
            finally:
                os._exit(0)
        inner_sender.close()
        inner_sender = None
        frame = _await_inner_trial_frame(inner_receiver, executor_pid, sender)
        cleanup_attempted = True
        try:
            _cleanup_trial_subtree()
        except _TrialCleanupFailed as exc:
            try:
                _send_trial_ipc_frame(
                    sender,
                    {
                        "kind": "cleanup_error",
                        "error_type": type(exc).__name__,
                        "message": str(exc)[:4096],
                    },
                )
            finally:
                raise SystemExit(72) from exc
        if frame is not None and not _trial_supervisor_stop_requested:
            _send_trial_ipc_frame(sender, frame)
    except (BrokenPipeError, EOFError):
        # The Core parent may have died.  Cleanup above remains authoritative;
        # there is no receiver left to notify.
        return
    finally:
        if executor_pid is not None and not cleanup_attempted:
            cleanup_attempted = True
            try:
                _cleanup_trial_subtree()
            except _TrialCleanupFailed as exc:
                try:
                    _send_trial_ipc_frame(
                        sender,
                        {
                            "kind": "cleanup_error",
                            "error_type": type(exc).__name__,
                            "message": str(exc)[:4096],
                        },
                    )
                except (BrokenPipeError, EOFError, OSError, ValueError):
                    pass
                os._exit(72)
        if inner_receiver is not None:
            inner_receiver.close()
        if inner_sender is not None:
            inner_sender.close()
        sender.close()


def _terminate_trial_process(process: Any, *, receiver: Any | None = None) -> None:
    """Ask the outer subreaper to clean its subtree, and require clean exit."""

    if process.pid is None:
        return
    if not process.is_alive():
        process.join(timeout=0)
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError:
            process.terminate()
        deadline = time.monotonic() + _TRIAL_TERMINATE_GRACE_SECONDS
        while process.is_alive() and time.monotonic() < deadline:
            # A bounded result can span multiple OS pipe buffers.  Continue
            # draining while cancellation/timeout waits for the subreaper, so
            # it cannot deadlock after it has already cleaned its descendants.
            if receiver is not None:
                try:
                    if receiver.poll(0.05):
                        _recv_trial_ipc_frame(receiver)
                except (EOFError, OSError, ValueError):
                    receiver = None
            else:
                process.join(timeout=0.05)
            process.join(timeout=0)
    if process.is_alive():
        raise _TrialCleanupFailed(
            f"Trial supervisor {process.pid} did not finish descendant cleanup"
        )
    process.join(timeout=0)
    if process.exitcode != 0:
        raise _TrialCleanupFailed(
            f"Trial supervisor {process.pid} exited without cleanup proof (code={process.exitcode})"
        )


def _await_clean_trial_supervisor_exit(process: Any) -> None:
    process.join(timeout=_TRIAL_TERMINATE_GRACE_SECONDS)
    if process.is_alive():
        raise _TrialCleanupFailed(
            f"Trial supervisor {process.pid} did not exit after cleanup proof"
        )
    process.join(timeout=0)
    if process.exitcode != 0:
        raise _TrialCleanupFailed(
            f"Trial supervisor {process.pid} exited without cleanup proof (code={process.exitcode})"
        )


def _execute_supervised_trial(
    request: TrialExecutionRequest,
    *,
    budget: _TrialExecutionBudget,
    cancel_check: CancelCheck | None,
    executor: TrialExecutor | None = None,
    observation_callback: Callable[[dict[str, Any]], None] | None = None,
    _context: Any | None = None,
) -> EvaluationTrial:
    """Execute one production Trial in a spawn-isolated, killable process."""

    if not math.isfinite(budget.seconds) or budget.seconds <= 0:
        raise _TrialExecutionDeadlineExceeded(scope=budget.scope, seconds=0.0)
    if executor is None:
        raise ResultContractError("supervisor.executor: required")
    effective_executor = executor
    context = _context or multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(
        target=_trial_process_main,
        args=(sender, request, effective_executor, os.getpid()),
        name=f"agentstrata-eval-trial-{_trial_id(request)[:48]}",
        daemon=False,
    )
    started = time.monotonic()
    deadline = started + budget.seconds
    startup_deadline = min(deadline, started + _TRIAL_STARTUP_TIMEOUT_SECONDS)
    latest_execution: dict[str, Any] | None = None

    def preserve_interrupted_timing() -> None:
        if latest_execution is None or observation_callback is None:
            return
        observed = dict(latest_execution)
        timing = observed.get('timing')
        if isinstance(timing, dict) and timing.get('state') == 'running':
            # Only retain elapsed time sampled by the executing child. A parent
            # deadline includes queueing/judging and is not Agent execution time.
            observed['timing'] = {**timing, 'state': 'partial'}
            observation_callback(observed)

    ready = False
    process_started = False
    try:
        process.start()
        process_started = True
        sender.close()
        while True:
            # Before the ready frame the child has not yet installed its
            # subreaper/signal contract.  Killing it in that narrow window can
            # only produce a signal exit, not proof that descendants were
            # cleaned.  Wait for ready, then consume the already-pending
            # cancellation immediately.
            if ready and cancel_check is not None and cancel_check():
                _terminate_trial_process(process, receiver=receiver)
                preserve_interrupted_timing()
                raise _TrialExecutionCancelled("Evaluation cancelled during an active Trial")
            now = time.monotonic()
            if now >= deadline:
                _terminate_trial_process(process, receiver=receiver)
                preserve_interrupted_timing()
                raise _TrialExecutionDeadlineExceeded(
                    scope=budget.scope,
                    seconds=budget.seconds,
                )
            if not ready and now >= startup_deadline:
                _terminate_trial_process(process, receiver=receiver)
                raise ResultContractError("supervised Trial process did not become ready")

            wait_seconds = min(0.1, deadline - now)
            try:
                has_message = receiver.poll(max(0.0, wait_seconds))
            except (EOFError, OSError) as exc:
                _terminate_trial_process(process, receiver=receiver)
                raise ResultContractError("supervised Trial evidence pipe failed") from exc
            if has_message:
                try:
                    message = _recv_trial_ipc_frame(receiver)
                except (EOFError, OSError, ValueError) as exc:
                    _terminate_trial_process(process, receiver=receiver)
                    raise ResultContractError("supervised Trial exited without evidence") from exc
                kind = message.get("kind")
                if kind == "ready":
                    if ready or message != {"kind": "ready", "pid": process.pid}:
                        _terminate_trial_process(process, receiver=receiver)
                        raise ResultContractError("supervised Trial returned an invalid ready frame")
                    ready = True
                    continue
                if kind == "observation":
                    execution = message.get("execution")
                    if not ready or set(message) != {"kind", "execution"} or not isinstance(execution, dict):
                        raise ResultContractError("supervised Trial returned an invalid observation")
                    latest_execution = json.loads(json.dumps(execution))
                    if observation_callback is not None:
                        observation_callback(execution)
                    continue
                if kind in {"startup_error", "error", "definition_drift"}:
                    _await_clean_trial_supervisor_exit(process)
                    if set(message) not in ({"kind", "error_type", "message"}, {"kind", "error_type", "message", "failure"}):
                        raise ResultContractError("supervised Trial returned a malformed error frame")
                    if kind == "definition_drift":
                        if message["error_type"] != _EvaluationDefinitionDrift.__name__:
                            raise ResultContractError(
                                "supervised Trial returned an invalid definition-drift frame"
                            )
                        raise _EvaluationDefinitionDrift(str(message["message"]))
                    if isinstance(message.get("failure"), dict):
                        raise PipelineFailure(EvaluationError(**message["failure"]))
                    raise PipelineFailure(EvaluationError("execution", "execution_error", f"{message['error_type']}: {message['message']}"))
                if kind == "cleanup_error":
                    process.join(timeout=_TRIAL_TERMINATE_GRACE_SECONDS)
                    if process.is_alive():
                        raise _TrialCleanupFailed(
                            f"Trial supervisor {process.pid} reported cleanup failure and stayed alive"
                        )
                    process.join(timeout=0)
                    raise _TrialCleanupFailed(
                        f"{message.get('error_type', 'TrialCleanupFailed')}: "
                        f"{message.get('message', 'Trial descendant cleanup failed')}"
                    )
                if kind == "result":
                    payload = message.get("trial")
                    if (
                        not ready
                        or set(message) != {"kind", "trial"}
                        or not isinstance(payload, Mapping)
                    ):
                        _terminate_trial_process(process, receiver=receiver)
                        raise ResultContractError("supervised Trial returned an invalid result frame")
                    try:
                        trial = _trial_from_dict(payload)
                        _assert_bounded_trial(trial)
                    except (TypeError, ValueError) as exc:
                        _terminate_trial_process(process, receiver=receiver)
                        raise ResultContractError(
                            "supervised Trial returned malformed canonical evidence"
                        ) from exc
                    _await_clean_trial_supervisor_exit(process)
                    return trial
                _terminate_trial_process(process, receiver=receiver)
                raise ResultContractError(f"supervised Trial returned unknown control frame {kind!r}")

            if not process.is_alive():
                # Drain a frame queued immediately before process exit once.
                if receiver.poll(0.05):
                    continue
                process.join(timeout=0)
                if process.exitcode != 0:
                    raise _TrialCleanupFailed(
                        f"Trial supervisor {process.pid} exited without cleanup proof "
                        f"(code={process.exitcode})"
                    )
                raise ResultContractError(
                    f"supervised Trial exited before returning evidence (code={process.exitcode})"
                )
    except BaseException as exc:
        if process.pid is not None and process.is_alive():
            try:
                _terminate_trial_process(process, receiver=receiver)
            except _TrialCleanupFailed as cleanup_exc:
                raise cleanup_exc from exc
        raise
    finally:
        receiver.close()
        sender.close()
        if process_started and not process.is_alive():
            process.join(timeout=0)
            process.close()
            if latest_execution and latest_execution.get("environment"):
                from chatcopilot.evals.environment_cleanup import cleanup_environment

                try:
                    with _preserved_environment():
                        load_evaluation_runtime(request.bot)
                        cleanup_environment(latest_execution["environment"])
                except Exception as exc:
                    raise _TrialCleanupFailed("benchmark environment cleanup was not confirmed") from exc


@contextmanager
def _preserved_environment() -> Iterator[None]:
    before = dict(os.environ)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(before)

