"""Canonical Evaluation domain and unified orchestration.

An Evaluation is the only top-level run resource.  Lifecycle status describes
the orchestration, while each Trial has an independent outcome.
"""

from __future__ import annotations

import hashlib
import hmac
from importlib import resources
import json
import math
import multiprocessing
import os
import re
import signal
import shutil
import stat
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Literal, Mapping, Sequence, TypeAlias

from chatcopilot.agent.backends.registry import backend_ids
from chatcopilot.agent.tools.registry import ToolMaterializationError, discover_tools
from chatcopilot.core.allowlists import parse_numeric_allowlist
from chatcopilot.core.config import ChatConfig, load_config
from chatcopilot.evals.artifact_ids import (
    contained_artifact_path,
    safe_artifact_component,
    trial_artifact_id,
)
from chatcopilot.evals.artifact_guard import (
    ArtifactIntegrityError,
    ArtifactIntegrityGuard,
)
from chatcopilot.evals.isolated_executor import (
    IsolatedTarget,
    IsolatedTrialRequest,
    execute_isolated_trial,
)
from chatcopilot.evals.evaluation_runtime import load_evaluation_runtime
from chatcopilot.evals.implementation_catalog import (
    comparison_implementation_snapshot,
    runtime_implementation_snapshot,
)
from chatcopilot.evals.manifest import (
    load_case_definitions,
    load_suite_manifest,
    resolve_suite_preset,
    suite_definition_snapshot,
)
from chatcopilot.evals.capability_executor import validate_capability_definition
from chatcopilot.evals.models import EvalCase, EvalCaseResult, to_jsonable
from chatcopilot.evals.paths import is_managed_evaluation_output
from chatcopilot.evals.profiles import ProfileCase, get_profile
from chatcopilot.evals.redaction import collect_env_secrets, redact_payload, sanitize_text
from chatcopilot.evals.plugins import CaseLoadContext, get_evaluation_plugin, get_plugin_binding
from chatcopilot.evals.registry import get_cases, get_manifest, get_standard
from chatcopilot.evals.runner import run_suite
from chatcopilot.external_tools.codex_cli import build_codex_command

EvaluationKind = Literal["comparison", "suite"]
EvaluationStatus = Literal[
    "queued",
    "running",
    "completed",
    "partial",
    "cancelled",
    "interrupted",
    "error",
]
TrialOutcome = Literal["passed", "failed", "skipped", "error"]
TargetExecutor = Literal[
    "direct_llm",
    "agent_configured",
    "agent_isolated",
    "acp_scenario",
    "qq_message_flow",
    "dry_run",
]
ComparisonPreset = Literal["quick", "standard", "custom"]

_MAX_TRIAL_ARTIFACT_BYTES = 2 * 1024 * 1024
_MAX_TRIAL_EVENTS = 512
_MAX_TRIAL_COLLECTION_ITEMS = 4096
_MAX_TRIAL_JSON_NODES = 20_000
_MAX_TRIAL_JSON_DEPTH = 12
_MAX_TRIAL_STRING_CHARS = 128 * 1024
_MAX_TRIAL_KEY_CHARS = 256
_MAX_TRIAL_IPC_FRAME_BYTES = _MAX_TRIAL_ARTIFACT_BYTES + 16 * 1024
_DEFAULT_TRIAL_TIMEOUT_SECONDS = 1200.0
_TRIAL_STARTUP_TIMEOUT_SECONDS = 15.0
_TRIAL_TERMINATE_GRACE_SECONDS = 5.0
_TRIAL_SUBTREE_TERM_GRACE_SECONDS = 0.5
_TRIAL_SUBTREE_KILL_GRACE_SECONDS = 2.0
_TRIAL_CLEANUP_ERROR_PREFIX = "trial_cleanup_failed:"
_ARTIFACT_INTEGRITY_ERROR_PREFIX = "artifact_integrity_violation:"

ProgressCallback = Callable[[dict[str, Any]], None]
CancelCheck = Callable[[], bool]
TrialExecutor = Callable[["TrialExecutionRequest"], "EvaluationTrial"]
_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_TERMINAL_STATUSES = frozenset({"completed", "partial", "cancelled", "interrupted", "error"})
_DEFAULT_COMPARISON_TARGETS = ("codex", "native")
_TIE_THRESHOLD = 0.05


@dataclass(frozen=True)
class EvaluationTarget:
    """Resolved and fingerprinted execution lane."""

    target_id: str
    label: str
    executor: TargetExecutor
    backend: str
    model: str
    reasoning_effort: str
    fingerprint: str
    config_fingerprint: str = ""


@dataclass(frozen=True)
class ComparisonEvaluationRequest:
    """Resolved request for a versioned Profile comparison."""

    evaluation_id: str
    kind: Literal["comparison"]
    bot: str
    profile: str
    preset: ComparisonPreset
    targets: tuple[str, ...]
    case_refs: tuple[str, ...]
    repetitions: int
    max_wall_seconds: float
    seed: int


@dataclass(frozen=True)
class SuiteEvaluationRequest:
    """Resolved request for one official or built-in benchmark Suite."""

    evaluation_id: str
    kind: Literal["suite"]
    bot: str
    suite: str
    case_ids: tuple[str, ...]
    preset: str
    repetitions: int
    max_wall_seconds: float
    seed: int
    options: dict[str, Any]
    confirm_external_write: bool
    dry_run: bool
    llm_judge: bool


EvaluationRequest: TypeAlias = ComparisonEvaluationRequest | SuiteEvaluationRequest


@dataclass(frozen=True)
class TrialExecutionRequest:
    """One executor invocation inside a complete target group."""

    evaluation_id: str
    kind: EvaluationKind
    bot: str
    output: Path
    suite_id: str
    profile: str
    profile_case: ProfileCase | None
    case: EvalCase
    dimension: str
    target: EvaluationTarget
    attempt: int
    order: int
    plugin_id: str = ""
    driver_id: str = ""
    dry_run: bool = False
    llm_judge: bool = False
    options: dict[str, Any] = field(default_factory=dict)
    confirm_external_write: bool = False
    max_execution_seconds: float = 0.0
    frozen_definition_snapshot: dict[str, Any] = field(default_factory=dict)
    frozen_definition_fingerprint: str = ""
    frozen_environment_fingerprint: str = ""


@dataclass(frozen=True)
class EvaluationTrial:
    """Coverage-complete evidence for one Case, attempt, and Target."""

    trial_id: str
    evaluation_id: str
    kind: EvaluationKind
    bot: str
    profile: str
    suite_id: str
    case_ref: str
    case_id: str
    dimension: str
    target_id: str
    target_fingerprint: str
    executor: TargetExecutor
    backend: str
    model: str
    reasoning_effort: str
    attempt: int
    order: int
    outcome: TrialOutcome
    score: float = 0.0
    max_score: float = 1.0
    passed: bool = False
    duration_seconds: float = 0.0
    final_text: str = ""
    stop_reason: str = ""
    started_at: str = ""
    finished_at: str = ""
    judge: dict[str, Any] | None = None
    events: tuple[dict[str, Any], ...] = ()
    usage_totals: dict[str, int] = field(default_factory=dict)
    tool_summary: dict[str, int] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)
    error: str = ""


@dataclass(frozen=True)
class CaseComparison:
    case_ref: str
    case_id: str
    dimension: str
    sample_size: int
    verdict: str
    targets: dict[str, dict[str, Any]]


@dataclass(frozen=True)
class EvaluationResult:
    """Authoritative top-level Evaluation result."""

    evaluation_id: str
    kind: EvaluationKind
    bot: str
    status: EvaluationStatus
    started_at: str
    finished_at: str
    duration_seconds: float
    profile: str = ""
    suite: str = ""
    preset: str = ""
    repetitions: int = 1
    max_wall_seconds: float = 0.0
    seed: int = 0
    targets: tuple[EvaluationTarget, ...] = ()
    selected_cases: tuple[str, ...] = ()
    trials: tuple[EvaluationTrial, ...] = ()
    comparisons: tuple[CaseComparison, ...] = ()
    dimensions: dict[str, Any] = field(default_factory=dict)
    summary: dict[str, Any] = field(default_factory=dict)
    config_snapshot: dict[str, Any] = field(default_factory=dict)
    error: str = ""


class EvaluationValidationError(ValueError):
    """Structured validation failure safe to expose through Console or CLI."""

    def __init__(
        self,
        message: str,
        *,
        checks: Sequence[Mapping[str, Any]],
        code: str = "evaluation_validation_failed",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.checks = [dict(item) for item in checks]

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "checks": self.checks}


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


@dataclass(frozen=True)
class _ResumeCheckpoint:
    trials: tuple[EvaluationTrial, ...] = ()
    started_at: str = ""
    duration_seconds: float = 0.0


def parse_evaluation_request(request: Mapping[str, Any] | EvaluationRequest) -> EvaluationRequest:
    """Strictly parse a discriminated request and resolve preset defaults."""

    if isinstance(request, (ComparisonEvaluationRequest, SuiteEvaluationRequest)):
        return request
    if not isinstance(request, Mapping):
        raise ValueError("evaluation request must be an object")
    kind = str(request.get("kind", "")).strip().lower()
    if kind == "comparison":
        return _parse_comparison_request(request)
    if kind == "suite":
        return _parse_suite_request(request)
    raise ValueError("kind must be 'comparison' or 'suite'")


def validate_evaluation(
    request: Mapping[str, Any] | EvaluationRequest,
) -> dict[str, Any]:
    """Validate without writing artifacts, starting workers, or preparing data."""

    checks: list[dict[str, Any]] = []
    try:
        parsed = parse_evaluation_request(request)
        checks.append(_check("request", "请求结构", True, f"kind={parsed.kind}"))
    except Exception as exc:  # noqa: BLE001
        checks.append(_check("request", "请求结构", False, str(exc), "修正评测请求字段"))
        return _validation_payload(False, checks, None, ())

    if parsed.kind == "comparison":
        targets = _validate_comparison(parsed, checks)
    else:
        targets = _validate_suite(parsed, checks)
    ready = bool(targets) and all(bool(item.get("ok")) for item in checks)
    return _validation_payload(ready, checks, parsed, targets)


def _case_execution_timeout(case: ProfileCase | EvalCase) -> float:
    """Return the trusted per-Case hard deadline, with a finite fallback."""

    eval_case = case.case if isinstance(case, ProfileCase) else case
    metadata = eval_case.metadata if isinstance(eval_case.metadata, Mapping) else {}
    definition = metadata.get("case_definition")
    if isinstance(definition, Mapping):
        policy = definition.get("policy")
        if isinstance(policy, Mapping):
            value = policy.get("timeout_seconds")
            if (
                not isinstance(value, bool)
                and isinstance(value, (int, float))
                and math.isfinite(float(value))
                and float(value) > 0
            ):
                return float(value)
    return _DEFAULT_TRIAL_TIMEOUT_SECONDS


def _trial_execution_budget(
    case: ProfileCase | EvalCase,
    *,
    max_wall_seconds: float,
    elapsed_seconds: float,
) -> _TrialExecutionBudget:
    """Take the minimum of the Case deadline and remaining Evaluation budget."""

    case_seconds = _case_execution_timeout(case)
    if max_wall_seconds <= 0:
        return _TrialExecutionBudget(seconds=case_seconds, scope="case")
    remaining = max_wall_seconds - elapsed_seconds
    if remaining <= 0:
        return _TrialExecutionBudget(seconds=0.0, scope="evaluation")
    if remaining <= case_seconds:
        return _TrialExecutionBudget(seconds=remaining, scope="evaluation")
    return _TrialExecutionBudget(seconds=case_seconds, scope="case")


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
        trial = executor(request)
        if not isinstance(trial, EvaluationTrial):
            raise TypeError("Trial executor did not return EvaluationTrial")
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
                },
            )
        except (BrokenPipeError, EOFError, OSError, ValueError):
            pass
    finally:
        sender.close()


def _await_inner_trial_frame(receiver: Any, executor_pid: int) -> dict[str, Any] | None:
    """Wait for inner evidence while remaining responsive to parent death."""

    while not _trial_supervisor_stop_requested:
        try:
            if receiver.poll(0.05):
                return _recv_trial_ipc_frame(receiver)
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
        frame = _await_inner_trial_frame(inner_receiver, executor_pid)
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
    _context: Any | None = None,
) -> EvaluationTrial:
    """Execute one production Trial in a spawn-isolated, killable process."""

    if not math.isfinite(budget.seconds) or budget.seconds <= 0:
        raise _TrialExecutionDeadlineExceeded(scope=budget.scope, seconds=0.0)
    effective_executor = executor or execute_evaluation_trial
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
                raise _TrialExecutionCancelled("Evaluation cancelled during an active Trial")
            now = time.monotonic()
            if now >= deadline:
                _terminate_trial_process(process, receiver=receiver)
                raise _TrialExecutionDeadlineExceeded(
                    scope=budget.scope,
                    seconds=budget.seconds,
                )
            if not ready and now >= startup_deadline:
                _terminate_trial_process(process, receiver=receiver)
                raise RuntimeError("supervised Trial process did not become ready")

            wait_seconds = min(0.1, deadline - now)
            try:
                has_message = receiver.poll(max(0.0, wait_seconds))
            except (EOFError, OSError) as exc:
                _terminate_trial_process(process, receiver=receiver)
                raise RuntimeError("supervised Trial evidence pipe failed") from exc
            if has_message:
                try:
                    message = _recv_trial_ipc_frame(receiver)
                except (EOFError, OSError, ValueError) as exc:
                    _terminate_trial_process(process, receiver=receiver)
                    raise RuntimeError("supervised Trial exited without evidence") from exc
                kind = message.get("kind")
                if kind == "ready":
                    if ready or message != {"kind": "ready", "pid": process.pid}:
                        _terminate_trial_process(process, receiver=receiver)
                        raise RuntimeError("supervised Trial returned an invalid ready frame")
                    ready = True
                    continue
                if kind in {"startup_error", "error", "definition_drift"}:
                    _await_clean_trial_supervisor_exit(process)
                    if set(message) != {"kind", "error_type", "message"}:
                        raise RuntimeError("supervised Trial returned a malformed error frame")
                    if kind == "definition_drift":
                        if message["error_type"] != _EvaluationDefinitionDrift.__name__:
                            raise RuntimeError(
                                "supervised Trial returned an invalid definition-drift frame"
                            )
                        raise _EvaluationDefinitionDrift(str(message["message"]))
                    raise RuntimeError(f"{message['error_type']}: {message['message']}")
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
                        raise RuntimeError("supervised Trial returned an invalid result frame")
                    try:
                        trial = _trial_from_dict(payload)
                        _assert_bounded_trial(trial)
                    except (TypeError, ValueError) as exc:
                        _terminate_trial_process(process, receiver=receiver)
                        raise RuntimeError(
                            "supervised Trial returned malformed canonical evidence"
                        ) from exc
                    _await_clean_trial_supervisor_exit(process)
                    return trial
                _terminate_trial_process(process, receiver=receiver)
                raise RuntimeError(f"supervised Trial returned unknown control frame {kind!r}")

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
                raise RuntimeError(
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


def _execute_trial_with_artifact_guard(
    request: TrialExecutionRequest,
    *,
    budget: _TrialExecutionBudget,
    cancel_check: CancelCheck | None,
    execute: TrialExecutor,
    supervise: bool,
    authority_claim_path: Path | None,
) -> EvaluationTrial:
    """Accept Trial evidence only if the parent-owned artifact surface stayed fixed."""

    with ArtifactIntegrityGuard.capture(
        request.output,
        evaluation_id=request.evaluation_id,
        claim_path=authority_claim_path,
    ) as guard:
        try:
            trial = (
                _execute_supervised_trial(
                    request,
                    budget=budget,
                    cancel_check=cancel_check,
                )
                if supervise
                else execute(request)
            )
        except _TrialCleanupFailed:
            # The supervisor could not prove that all potential writers have
            # stopped.  Reading the authority surface now would itself create
            # a false integrity claim; the existing cleanup quarantine wins.
            raise
        except BaseException:
            # Timeout, cancellation, definition drift and ordinary executor
            # failures all return only after the supervisor has cleaned the
            # Trial tree.  Verify before the original failure is classified.
            guard.verify()
            raise
        guard.verify()
        return trial


def run_evaluation(
    request: Mapping[str, Any] | EvaluationRequest,
    *,
    output: Path,
    progress_callback: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
    trial_executor: TrialExecutor | None = None,
    resume: bool = False,
    managed: bool = False,
    authority_claim_path: Path | None = None,
) -> EvaluationResult:
    """Run case × attempt × targets with complete target-group checkpoints."""

    validation = validate_evaluation(request)
    if not validation["ready"]:
        failed = [item for item in validation["checks"] if not item.get("ok")]
        message = "; ".join(str(item.get("detail", "")) for item in failed)
        raise EvaluationValidationError(
            message or "evaluation validation failed",
            checks=validation["checks"],
            code=str(validation.get("code") or "evaluation_validation_failed"),
        )
    parsed = parse_evaluation_request(request)
    targets = tuple(_target_from_dict(item) for item in validation["targets"])
    output = output.expanduser().resolve()
    cases = _execution_cases(parsed)
    snapshot = _config_snapshot(parsed, targets, cases)
    if managed and resume:
        raise ValueError("managed Evaluation cannot use the standalone resume path")
    if resume:
        checkpoint = _resume_checkpoint(
            output=output,
            request=parsed,
            targets=targets,
            cases=cases,
            config_snapshot=snapshot,
        )
    else:
        _validate_fresh_output(
            output=output,
            request=parsed,
            targets=targets,
            managed=managed,
        )
        checkpoint = _ResumeCheckpoint()
    _ensure_private_dir(output)
    if not resume and not managed:
        _persist_request(parsed, targets, output)

    started_clock = time.monotonic()
    started_at = checkpoint.started_at or _utc_now()
    trials: list[EvaluationTrial] = list(checkpoint.trials)
    status: EvaluationStatus = "running"
    error = ""
    repetitions = parsed.repetitions
    max_wall_seconds = parsed.max_wall_seconds
    seed = parsed.seed
    total_trials = len(cases) * repetitions * len(targets)
    complete_groups = _complete_group_keys(trials, targets)
    execute = trial_executor or execute_evaluation_trial
    supervise_trials = trial_executor is None
    result = _build_result(
        request=parsed,
        targets=targets,
        cases=cases,
        trials=trials,
        status=status,
        started_at=started_at,
        started_clock=started_clock,
        previous_duration_seconds=checkpoint.duration_seconds,
        config_snapshot=snapshot,
    )
    _persist_evaluation(result, output, write_state=not managed)
    _record_event(
        output,
        progress_callback,
        event="evaluation_started",
        evaluation_id=parsed.evaluation_id,
        kind=parsed.kind,
        status=status,
        completed_trials=len(trials),
        total_trials=total_trials,
    )
    # Validation and durable bootstrap writes are control-plane work. The
    # execution budget begins only after they are complete; resumed runtime is
    # still accounted for through checkpoint.duration_seconds.
    started_clock = time.monotonic()

    group_index = 0
    try:
        stop = False
        for case in cases:
            case_ref = _case_ref(
                case,
                suite_id=parsed.suite if parsed.kind == "suite" else "",
            )
            for attempt in range(1, repetitions + 1):
                group_key = (case_ref, attempt)
                if group_key in complete_groups:
                    group_index += 1
                    continue
                if cancel_check is not None and cancel_check():
                    status = "cancelled"
                    stop = True
                    break
                elapsed_seconds = checkpoint.duration_seconds + time.monotonic() - started_clock
                if max_wall_seconds > 0 and elapsed_seconds >= max_wall_seconds:
                    status = "partial"
                    stop = True
                    break

                ordered_targets = list(targets)
                if parsed.kind == "comparison" and (seed + group_index) % 2 == 1:
                    ordered_targets.reverse()
                group_index += 1
                group: list[EvaluationTrial] = []
                group_requests: list[TrialExecutionRequest] = []
                group_aborted = False
                group_quarantined = False
                for order, target in enumerate(ordered_targets, start=1):
                    execution_request = _trial_request(
                        request=parsed,
                        output=output,
                        case=case,
                        target=target,
                        attempt=attempt,
                        order=order,
                        config_snapshot=snapshot,
                    )
                    if supervise_trials:
                        if cancel_check is not None and cancel_check():
                            status = "cancelled"
                            stop = True
                            group_aborted = True
                            break
                        elapsed_seconds = (
                            checkpoint.duration_seconds + time.monotonic() - started_clock
                        )
                        budget = _trial_execution_budget(
                            case,
                            max_wall_seconds=max_wall_seconds,
                            elapsed_seconds=elapsed_seconds,
                        )
                        if budget.seconds <= 0:
                            status = "partial"
                            stop = True
                            group_aborted = True
                            break
                        execution_request = replace(
                            execution_request,
                            max_execution_seconds=budget.seconds,
                        )
                    else:
                        budget = _TrialExecutionBudget(
                            seconds=_case_execution_timeout(case),
                            scope="case",
                        )
                    group_requests.append(execution_request)
                    _reset_trial_workspace(execution_request)
                    _record_event(
                        output,
                        progress_callback,
                        event="trial_started",
                        evaluation_id=parsed.evaluation_id,
                        case_ref=case_ref,
                        target_id=target.target_id,
                        target_fingerprint=target.fingerprint,
                        attempt=attempt,
                        order=order,
                        completed_trials=len(trials),
                        total_trials=total_trials,
                    )
                    try:
                        trial = _execute_trial_with_artifact_guard(
                            execution_request,
                            budget=budget,
                            cancel_check=cancel_check,
                            execute=execute,
                            supervise=supervise_trials,
                            authority_claim_path=authority_claim_path,
                        )
                    except _TrialExecutionCancelled:
                        status = "cancelled"
                        stop = True
                        group_aborted = True
                        _record_event(
                            output,
                            progress_callback,
                            event="trial_cancelled",
                            evaluation_id=parsed.evaluation_id,
                            case_ref=case_ref,
                            target_id=target.target_id,
                            target_fingerprint=target.fingerprint,
                            attempt=attempt,
                            completed_trials=len(trials),
                            total_trials=total_trials,
                        )
                        break
                    except _TrialCleanupFailed as exc:
                        status = "error"
                        error = f"{_TRIAL_CLEANUP_ERROR_PREFIX} {exc}"
                        stop = True
                        group_aborted = True
                        group_quarantined = True
                        _record_event(
                            output,
                            progress_callback,
                            event="trial_cleanup_failed",
                            evaluation_id=parsed.evaluation_id,
                            case_ref=case_ref,
                            target_id=target.target_id,
                            target_fingerprint=target.fingerprint,
                            attempt=attempt,
                            completed_trials=len(trials),
                            total_trials=total_trials,
                        )
                        break
                    except ArtifactIntegrityError as exc:
                        status = "error"
                        error = f"{_ARTIFACT_INTEGRITY_ERROR_PREFIX} {exc}"
                        stop = True
                        group_aborted = True
                        group_quarantined = True
                        break
                    except _EvaluationDefinitionDrift as exc:
                        status = "error"
                        error = f"evaluation definition drift: {exc}"
                        stop = True
                        group_aborted = True
                        _record_event(
                            output,
                            progress_callback,
                            event="evaluation_definition_drift",
                            evaluation_id=parsed.evaluation_id,
                            case_ref=case_ref,
                            target_id=target.target_id,
                            target_fingerprint=target.fingerprint,
                            attempt=attempt,
                            completed_trials=len(trials),
                            total_trials=total_trials,
                        )
                        break
                    except _TrialExecutionDeadlineExceeded as exc:
                        if exc.scope == "evaluation":
                            status = "partial"
                            stop = True
                            group_aborted = True
                            _record_event(
                                output,
                                progress_callback,
                                event="evaluation_budget_exhausted",
                                evaluation_id=parsed.evaluation_id,
                                case_ref=case_ref,
                                target_id=target.target_id,
                                target_fingerprint=target.fingerprint,
                                attempt=attempt,
                                completed_trials=len(trials),
                                total_trials=total_trials,
                            )
                            break
                        trial = _error_trial(execution_request, exc)
                    except Exception as exc:  # noqa: BLE001
                        trial = _error_trial(execution_request, exc)
                    trial = replace(trial, trial_id=_trial_id(execution_request))
                    try:
                        sanitized_trial = _sanitize_trial(trial, output=output)
                    except Exception as exc:  # noqa: BLE001
                        sanitized_trial = _sanitize_trial(
                            _error_trial(
                                execution_request,
                                ValueError(
                                    "trial evidence failed Core integrity limits: "
                                    f"{type(exc).__name__}: {exc}"
                                ),
                            ),
                            output=output,
                        )
                        sanitized_trial = replace(
                            sanitized_trial,
                            trial_id=_trial_id(execution_request),
                        )
                    group.append(sanitized_trial)
                    _record_event(
                        output,
                        progress_callback,
                        event="trial_completed",
                        evaluation_id=parsed.evaluation_id,
                        case_ref=case_ref,
                        target_id=target.target_id,
                        target_fingerprint=target.fingerprint,
                        attempt=attempt,
                        outcome=group[-1].outcome,
                        completed_trials=len(trials) + len(group),
                        total_trials=total_trials,
                    )

                # A checkpoint either contains every Target in the group or none.
                if group_aborted:
                    if not group_quarantined:
                        for pending_request in group_requests:
                            _discard_trial_workspace(pending_request)
                    _record_event(
                        output,
                        progress_callback,
                        event=(
                            "target_group_quarantined"
                            if group_quarantined
                            else "target_group_discarded"
                        ),
                        evaluation_id=parsed.evaluation_id,
                        kind=parsed.kind,
                        status=status,
                        case_ref=case_ref,
                        attempt=attempt,
                        completed_trials=len(trials),
                        total_trials=total_trials,
                    )
                elif len(group) == len(targets):
                    trials.extend(group)
                    complete_groups.add(group_key)
                result = _build_result(
                    request=parsed,
                    targets=targets,
                    cases=cases,
                    trials=trials,
                    status=status,
                    started_at=started_at,
                    started_clock=started_clock,
                    previous_duration_seconds=checkpoint.duration_seconds,
                    config_snapshot=snapshot,
                )
                _persist_evaluation(result, output, write_state=not managed)
                if not group_aborted:
                    _record_event(
                        output,
                        progress_callback,
                        event="target_group_completed",
                        evaluation_id=parsed.evaluation_id,
                        kind=parsed.kind,
                        status=status,
                        case_ref=case_ref,
                        attempt=attempt,
                        completed_trials=len(trials),
                        total_trials=total_trials,
                    )
                if stop:
                    break
            if stop:
                break
    except KeyboardInterrupt:
        status = "interrupted"
        error = "evaluation interrupted"
    except Exception as exc:  # noqa: BLE001
        status = "error"
        error = f"{type(exc).__name__}: {exc}"

    if status == "running":
        status = "completed"
    result = _build_result(
        request=parsed,
        targets=targets,
        cases=cases,
        trials=trials,
        status=status,
        started_at=started_at,
        started_clock=started_clock,
        previous_duration_seconds=checkpoint.duration_seconds,
        config_snapshot=snapshot,
        error=error,
        finished=True,
    )
    _persist_evaluation(result, output, write_state=not managed)
    _record_event(
        output,
        progress_callback,
        event="evaluation_completed",
        evaluation_id=parsed.evaluation_id,
        kind=parsed.kind,
        status=result.status,
        completed_trials=len(result.trials),
        total_trials=total_trials,
    )
    return result


def _load_current_suite_manifest(suite_id: str) -> Any:
    if not _ID_PATTERN.fullmatch(suite_id):
        raise ValueError("Suite id is invalid")
    suites_root = resources.files("chatcopilot.evals").joinpath("suites")
    if not isinstance(suites_root, Path):
        raise ValueError("evaluation suites root must be filesystem-backed")
    suite_dir = suites_root.joinpath(suite_id)
    return load_suite_manifest(suite_dir.joinpath("manifest.yaml"), suite_dir=suite_dir)


def _current_suite_target(request: TrialExecutionRequest) -> EvaluationTarget:
    if request.target.executor == "dry_run":
        current_config_fingerprint = _hash_json({"executor": "dry_run", "suite": request.suite_id})
    else:
        if not request.bot:
            raise ValueError("configured Suite Target has no BotSpec")
        with _preserved_environment():
            runtime = load_evaluation_runtime(request.bot)
            config = load_config(env_prefix=runtime.spec.llm.env_prefix)
            current_config_fingerprint = _runtime_behavior_fingerprint(
                runtime,
                config,
                backend=request.target.backend,
            )
    if current_config_fingerprint != request.target.config_fingerprint:
        raise _EvaluationDefinitionDrift(
            "Target runtime/config implementation changed "
            f"(expected={request.target.config_fingerprint[:12]}, "
            f"actual={current_config_fingerprint[:12]})"
        )
    current = _make_target(
        target_id=request.target.target_id,
        label=request.target.label,
        executor=request.target.executor,
        backend=request.target.backend,
        model=request.target.model,
        reasoning_effort=request.target.reasoning_effort,
        config_fingerprint=current_config_fingerprint,
    )
    if current.fingerprint != request.target.fingerprint:
        raise _EvaluationDefinitionDrift("Target identity does not match its frozen fingerprint")
    return current


def _assert_suite_trial_definition_current(request: TrialExecutionRequest) -> None:
    """Recompute the complete Suite identity without preparing data or executing tools."""

    if request.kind != "suite":
        return
    expected = request.frozen_definition_snapshot
    if not isinstance(expected, dict) or not expected:
        raise _EvaluationDefinitionDrift("Suite Trial has no frozen definition snapshot")
    if _hash_json(expected) != request.frozen_definition_fingerprint:
        raise _EvaluationDefinitionDrift("frozen Suite definition fingerprint is inconsistent")
    environment_identity = expected.get("environment_identity")
    if (
        not isinstance(environment_identity, Mapping)
        or environment_identity.get("private_runtime_configuration_sha256")
        != request.frozen_environment_fingerprint
    ):
        raise _EvaluationDefinitionDrift("frozen Suite environment fingerprint is inconsistent")
    expected_base_fingerprint = expected.get("base_fingerprint")
    expected_base = dict(expected)
    expected_base.pop("base_fingerprint", None)
    expected_base.pop("environment_identity", None)
    if expected_base_fingerprint != _hash_json(expected_base):
        raise _EvaluationDefinitionDrift("frozen Suite base fingerprint is inconsistent")

    records = expected.get("cases")
    if not isinstance(records, list) or not records:
        raise _EvaluationDefinitionDrift("frozen Suite definition has no selected Cases")
    expected_case_ids: list[str] = []
    expected_case_hashes: dict[str, str] = {}
    for record in records:
        if not isinstance(record, Mapping):
            raise _EvaluationDefinitionDrift("frozen Suite Case identity is malformed")
        case_id = str(record.get("case_id") or "")
        digest = str(record.get("definition_sha256") or "")
        if (
            not case_id
            or case_id in expected_case_hashes
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
        ):
            raise _EvaluationDefinitionDrift("frozen Suite Case identity is malformed")
        expected_case_ids.append(case_id)
        expected_case_hashes[case_id] = digest
    if request.case.case_id not in expected_case_hashes:
        raise _EvaluationDefinitionDrift("Trial Case is outside the frozen Suite selection")
    if _hash_json(to_jsonable(request.case)) != expected_case_hashes[request.case.case_id]:
        raise _EvaluationDefinitionDrift("Trial Case differs from the parent-frozen EvalCase")

    expected_target = expected.get("target_fingerprint")
    try:
        frozen_target = _current_suite_target(request)
    except _EvaluationDefinitionDrift:
        raise
    except Exception as exc:
        raise _EvaluationDefinitionDrift(
            f"current Target identity could not be verified ({type(exc).__name__})"
        ) from exc
    current_target_material = {frozen_target.target_id: frozen_target.fingerprint}
    if expected_target != current_target_material:
        raise _EvaluationDefinitionDrift("frozen Suite Target set is inconsistent")

    try:
        manifest = _load_current_suite_manifest(request.suite_id)
        if manifest.status != "implemented":
            raise ValueError("Suite is no longer implemented")
        plugin = get_evaluation_plugin(manifest.plugin_id)
        loaded_cases = plugin.load_cases(
            CaseLoadContext(
                manifest=manifest,
                auto_prepare=False,
            )
        )
        selected_cases = _select_suite_cases(loaded_cases, expected_case_ids)
        current = suite_definition_snapshot(
            manifest,
            plugin,
            selected_cases,
            target_fingerprint=current_target_material,
        )
        current["base_fingerprint"] = _hash_json(current)
        private_runtime_configuration = _private_runtime_configuration_snapshot(request.bot)
        current_environment_fingerprint = _hash_json(private_runtime_configuration)
        current["environment_identity"] = {
            "private_runtime_configuration_sha256": current_environment_fingerprint,
        }
    except _EvaluationDefinitionDrift:
        raise
    except Exception as exc:
        raise _EvaluationDefinitionDrift(
            f"current Suite identity could not be verified ({type(exc).__name__})"
        ) from exc

    if current_environment_fingerprint != request.frozen_environment_fingerprint:
        raise _EvaluationDefinitionDrift("Suite private runtime environment changed")
    if current != expected or _hash_json(current) != request.frozen_definition_fingerprint:
        raise _EvaluationDefinitionDrift("Suite definition or trusted implementation changed")


def execute_evaluation_trial(request: TrialExecutionRequest) -> EvaluationTrial:
    """Dispatch explicitly to the supported executor policies."""

    executor = request.driver_id or request.target.executor
    if executor == "agent_isolated" and request.profile_case is not None:
        isolated = execute_isolated_trial(
            IsolatedTrialRequest(
                bot=request.bot,
                evaluation_id=request.evaluation_id,
                output=request.output,
                profile_case=request.profile_case,
                target=IsolatedTarget(
                    target_id=request.target.target_id,
                    backend=request.target.backend,
                    label=request.target.label,
                    fingerprint=request.target.fingerprint,
                    model=request.target.model,
                    reasoning_effort=request.target.reasoning_effort,
                ),
                attempt=request.attempt,
                order=request.order,
            )
        )
        return EvaluationTrial(
            trial_id=isolated.trial_id,
            evaluation_id=request.evaluation_id,
            kind=request.kind,
            bot=request.bot,
            profile=request.profile,
            suite_id=isolated.suite_id,
            case_ref=isolated.case_ref,
            case_id=isolated.case_id,
            dimension=isolated.dimension,
            target_id=request.target.target_id,
            target_fingerprint=request.target.fingerprint,
            executor="agent_isolated",
            backend=request.target.backend,
            model=request.target.model,
            reasoning_effort=request.target.reasoning_effort,
            attempt=request.attempt,
            order=request.order,
            outcome=_normalize_outcome(isolated.outcome),
            score=isolated.score,
            max_score=1.0,
            passed=isolated.passed,
            duration_seconds=isolated.duration_seconds,
            final_text=isolated.final_text,
            stop_reason=isolated.stop_reason,
            started_at=isolated.started_at,
            finished_at=isolated.finished_at,
            judge=isolated.judge,
            events=isolated.events,
            usage_totals=isolated.usage_totals or {},
            tool_summary=isolated.tool_summary or {},
            evidence=isolated.evidence or {},
            error=isolated.error,
        )

    if executor not in {
        "direct_llm",
        "agent_configured",
        "agent_isolated",
        "acp_scenario",
        "qq_message_flow",
        "dry_run",
    }:
        raise ValueError(f"unsupported evaluation executor: {executor}")
    _assert_suite_trial_definition_current(request)
    run = run_suite(
        request.suite_id,
        bot=request.bot or None,
        dry_run=executor == "dry_run",
        llm_judge=request.llm_judge,
        case_ids=[request.case.case_id],
        options=request.options,
        confirm_external_write=request.confirm_external_write,
        workspace_root=contained_artifact_path(
            request.output,
            "workspaces",
            _trial_id(request),
        ),
        _frozen_cases=(request.case,),
    )
    _assert_suite_trial_definition_current(request)
    if len(run.cases) != 1:
        raise ValueError(
            f"{request.suite_id}:{request.case.case_id} produced {len(run.cases)} results"
        )
    return _trial_from_case_result(request, run.cases[0])


def evaluation_result_to_dict(result: EvaluationResult) -> dict[str, Any]:
    """Return a stable JSON-ready result shape for Console and CLI."""

    payload = to_jsonable(result)
    return redact_payload(
        payload,
        secrets=collect_env_secrets(),
        roots={"repository": Path.cwd()},
    )


def aggregate_comparison(
    trials: Sequence[EvaluationTrial],
    cases: Sequence[ProfileCase],
    targets: Sequence[EvaluationTarget],
) -> tuple[tuple[CaseComparison, ...], dict[str, Any], dict[str, Any]]:
    """Aggregate only complete, outcome-bearing target pairs."""

    comparisons: list[CaseComparison] = []
    target_ids = tuple(target.target_id for target in targets)
    for item in cases:
        case_trials = [trial for trial in trials if trial.case_ref == item.ref]
        complete_attempts = _valid_complete_attempts(case_trials, targets)
        comparable = [trial for trial in case_trials if trial.attempt in complete_attempts]
        target_stats = {
            target.target_id: _target_statistics(
                [trial for trial in comparable if trial.target_id == target.target_id]
            )
            for target in targets
        }
        verdict = _verdict(target_stats, target_ids, bool(complete_attempts))
        comparisons.append(
            CaseComparison(
                case_ref=item.ref,
                case_id=item.case_id,
                dimension=item.dimension,
                sample_size=len(complete_attempts) if len(targets) == 2 else 0,
                verdict=verdict,
                targets=target_stats,
            )
        )

    dimensions: dict[str, Any] = {}
    for dimension in dict.fromkeys(item.dimension for item in cases):
        dimension_cases = [item for item in cases if item.dimension == dimension]
        refs = {item.ref for item in dimension_cases}
        dimension_trials = [trial for trial in trials if trial.case_ref in refs]
        complete_by_ref = {
            item.ref: _valid_complete_attempts(
                [trial for trial in dimension_trials if trial.case_ref == item.ref],
                targets,
            )
            for item in dimension_cases
        }
        comparable = [
            trial
            for trial in dimension_trials
            if trial.attempt in complete_by_ref.get(trial.case_ref, set())
        ]
        target_stats = {
            target.target_id: _target_statistics(
                [trial for trial in comparable if trial.target_id == target.target_id]
            )
            for target in targets
        }
        sample_size = sum(len(value) for value in complete_by_ref.values())
        dimensions[dimension] = {
            "case_count": len(dimension_cases),
            "sample_size": sample_size if len(targets) == 2 else 0,
            "verdict": _verdict(target_stats, target_ids, sample_size > 0),
            "targets": target_stats,
        }

    case_verdicts = [item.verdict for item in comparisons]
    wins = (
        {target.target_id: case_verdicts.count(target.target_id) for target in targets}
        if len(targets) == 2
        else {}
    )
    summary = {
        "case_count": len(cases),
        "trial_count": len(trials),
        "paired_attempt_count": (
            sum(item.sample_size for item in comparisons) if len(targets) == 2 else 0
        ),
        "wins": wins,
        "ties": case_verdicts.count("tie") if len(targets) == 2 else 0,
        "inconclusive": case_verdicts.count("inconclusive"),
        "tie_threshold": _TIE_THRESHOLD,
        "outcomes": _outcome_counts(trials),
        "cost": "unknown",
        "scope_note": "Directional Profile comparison; not a general intelligence ranking.",
    }
    return tuple(comparisons), dimensions, summary


def _parse_comparison_request(request: Mapping[str, Any]) -> ComparisonEvaluationRequest:
    allowed = {
        "evaluation_id",
        "kind",
        "bot",
        "profile",
        "preset",
        "targets",
        "case_refs",
        "repetitions",
        "max_wall_seconds",
        "seed",
    }
    _reject_extra_fields(request, allowed)
    evaluation_id = _evaluation_id(request.get("evaluation_id"))
    bot = _required_text(request.get("bot"), "bot")
    profile_id = str(request.get("profile") or "agent-comparison-mvp").strip().lower()
    profile = get_profile(profile_id)
    preset = str(request.get("preset") or "quick").strip().lower()
    if preset not in {"quick", "standard", "custom"}:
        raise ValueError("preset must be quick, standard, or custom")
    override_fields = {
        "targets",
        "case_refs",
        "repetitions",
        "max_wall_seconds",
        "seed",
    }
    targets: tuple[str, ...]
    case_refs: tuple[str, ...]
    supplied_overrides = sorted(field for field in override_fields if field in request)
    if preset in {"quick", "standard"}:
        if supplied_overrides:
            raise ValueError(
                f"{preset} preset does not accept overrides: {', '.join(supplied_overrides)}"
            )
        mode = profile.modes.get(preset)
        if mode is None:
            raise ValueError(f"profile {profile_id} does not define preset {preset}")
        targets = _DEFAULT_COMPARISON_TARGETS
        case_refs = tuple(item.ref for item in profile.cases)
        repetitions = mode.repetitions
        max_wall_seconds = float(mode.max_wall_seconds)
        seed = profile.default_seed
    else:
        missing = sorted(field for field in override_fields if field not in request)
        if missing:
            raise ValueError(f"custom preset requires: {', '.join(missing)}")
        targets = _unique_texts(request.get("targets"), "targets")
        case_refs = _unique_texts(request.get("case_refs"), "case_refs")
        repetitions = _positive_int(request.get("repetitions"), "repetitions")
        max_wall_seconds = _positive_number(request.get("max_wall_seconds"), "max_wall_seconds")
        seed = _integer(request.get("seed"), "seed")
    known_cases = {item.ref for item in profile.cases}
    unknown = [value for value in case_refs if value not in known_cases]
    if unknown:
        raise ValueError(f"unknown Profile Case refs: {', '.join(unknown)}")
    return ComparisonEvaluationRequest(
        evaluation_id=evaluation_id,
        kind="comparison",
        bot=bot,
        profile=profile.profile_id,
        preset=preset,  # type: ignore[arg-type]
        targets=targets,
        case_refs=case_refs,
        repetitions=repetitions,
        max_wall_seconds=max_wall_seconds,
        seed=seed,
    )


def _parse_suite_request(request: Mapping[str, Any]) -> SuiteEvaluationRequest:
    allowed = {
        "evaluation_id",
        "kind",
        "bot",
        "suite",
        "case_ids",
        "preset",
        "repetitions",
        "max_wall_seconds",
        "seed",
        "options",
        "confirm_external_write",
        "dry_run",
        "llm_judge",
    }
    _reject_extra_fields(request, allowed)
    suite = _required_text(request.get("suite"), "suite").lower().replace("_", "-")
    manifest = get_manifest(suite)
    dry_run = _strict_bool(request.get("dry_run", False), "dry_run")
    llm_judge = _strict_bool(request.get("llm_judge", False), "llm_judge")
    if llm_judge and suite != "gaia":
        raise ValueError("llm_judge is supported only for GAIA")
    raw_case_ids = request.get("case_ids")
    requested_case_ids = (
        ()
        if raw_case_ids is None or raw_case_ids == [] or raw_case_ids == ()
        else _unique_texts(raw_case_ids, "case_ids")
    )
    raw_preset = str(request.get("preset") or "").strip().lower().replace("_", "-")
    if raw_preset and not _ID_PATTERN.fullmatch(raw_preset):
        raise ValueError("preset must use letters, digits, '_' or '-'")
    preset = raw_preset or ("custom" if requested_case_ids else manifest.default_preset)
    available_cases = get_cases(suite, auto_prepare=False)
    # Legacy official suites intentionally keep an omitted selection as an
    # empty tuple (meaning "all cases"). Product suites with a declared
    # default preset must materialize that preset so the exact Case set is
    # durable in the request and definition fingerprint.
    case_ids = (
        resolve_suite_preset(
            manifest,
            preset=preset,
            case_ids=requested_case_ids,
            available_case_ids=(case.case_id for case in available_cases),
        )
        if requested_case_ids or preset
        else ()
    )
    repetitions = _positive_int(request.get("repetitions", 1), "repetitions")
    if repetitions > 10:
        raise ValueError("repetitions must be at most 10")
    max_wall_seconds = _finite_non_negative_number(
        request.get("max_wall_seconds", 0),
        "max_wall_seconds",
    )
    if max_wall_seconds > 21600:
        raise ValueError("max_wall_seconds must be at most 21600")
    seed = _integer(request.get("seed", 0), "seed")
    options = _suite_options(manifest, request.get("options", {}))
    declared_options = {item.name for item in manifest.options}
    if "dry_run" in declared_options:
        options["dry_run"] = dry_run
    if "llm_judge" in declared_options:
        options["llm_judge"] = llm_judge
    confirm_external_write = _strict_bool(
        request.get("confirm_external_write", False),
        "confirm_external_write",
    )
    if confirm_external_write:
        raise ValueError(
            "Evaluation does not support external writes; use 'bot external-check' "
            "for platform connectivity checks"
        )
    bot = str(request.get("bot") or "").strip()
    if not dry_run and not bot:
        raise ValueError("bot is required unless dry_run is true")
    return SuiteEvaluationRequest(
        evaluation_id=_evaluation_id(request.get("evaluation_id")),
        kind="suite",
        bot=bot,
        suite=suite,
        case_ids=case_ids,
        preset=preset or "custom",
        repetitions=repetitions,
        max_wall_seconds=max_wall_seconds,
        seed=seed,
        options=options,
        confirm_external_write=confirm_external_write,
        dry_run=dry_run,
        llm_judge=llm_judge,
    )


def _validate_comparison(
    request: ComparisonEvaluationRequest,
    checks: list[dict[str, Any]],
) -> tuple[EvaluationTarget, ...]:
    profile = get_profile(request.profile)
    cases = [item for item in profile.cases if item.ref in set(request.case_refs)]
    checks.append(
        _check(
            "profile",
            "Profile 与固定 Case",
            len(cases) == len(request.case_refs),
            f"profile={profile.profile_id}, cases={len(cases)}",
        )
    )
    targets: list[EvaluationTarget] = []
    try:
        with _preserved_environment():
            runtime = load_evaluation_runtime(request.bot)
            config = load_config(env_prefix=runtime.spec.llm.env_prefix)
            checks.append(_check("botspec", "BotSpec", True, runtime.source_path.parent.name))
            for target_id in request.targets:
                config_fingerprint = _runtime_behavior_fingerprint(
                    runtime,
                    config,
                    backend=target_id,
                )
                target, target_check = _isolated_target(
                    target_id,
                    config,
                    config_fingerprint=config_fingerprint,
                )
                checks.append(target_check)
                targets.append(target)
    except Exception as exc:  # noqa: BLE001
        checks.append(_check("botspec", "BotSpec", False, str(exc), "检查 BotSpec 与 local.env"))
        return ()

    for item in cases:
        allowed = item.case.metadata.get("allowed_tools", [])
        fixture_ok = item.case.category != "code" or bool(item.case.metadata.get("fixture_files"))
        checks.append(
            _check(
                f"policy:{item.ref}",
                f"隔离策略 · {item.case_id}",
                isinstance(allowed, list) and fixture_ok,
                f"allow={','.join(str(value) for value in allowed) or 'none'}",
                "为 Profile Case 声明工具白名单和代码 fixture",
            )
        )
    return tuple(targets)


def _validate_suite(
    request: SuiteEvaluationRequest,
    checks: list[dict[str, Any]],
) -> tuple[EvaluationTarget, ...]:
    standard = get_standard(request.suite)
    manifest = get_manifest(request.suite)
    if manifest.status != "implemented":
        checks.append(
            _check(
                "suite_unavailable",
                "Suite 可用性",
                False,
                f"suite={manifest.suite_id}, status={manifest.status}",
                manifest.setup_hint or "该 Suite 尚未实现",
            )
        )
        return ()
    try:
        loaded = get_cases(standard.suite_id, auto_prepare=False)
        selected = _select_suite_cases(loaded, request.case_ids)
        ready = bool(selected)
        detail = f"suite={standard.suite_id}, cases={len(selected)}"
        if not loaded and standard.requires_external_data:
            detail = standard.setup_hint or "official data is not prepared"
        checks.append(
            _check(
                "suite",
                "Suite 数据与 Case",
                ready,
                detail,
                "先准备官方数据或修正 case_ids",
            )
        )
    except Exception as exc:  # noqa: BLE001
        checks.append(_check("suite", "Suite 数据与 Case", False, str(exc), "修正数据或 Case"))
        return ()

    try:
        for case in selected:
            plugin_id, driver_id = _case_plugin_driver(manifest, case)
            binding = get_plugin_binding(plugin_id)
            if driver_id not in binding.allowed_drivers:
                raise ValueError(f"plugin {plugin_id} does not allow driver {driver_id}")
        checks.append(
            _check(
                "plugin_bindings",
                "受信插件与 Driver",
                True,
                f"cases={len(selected)}, api=1",
            )
        )
    except Exception as exc:  # noqa: BLE001
        checks.append(
            _check(
                "plugin_bindings",
                "受信插件与 Driver",
                False,
                str(exc),
                "修正 manifest、Case 或静态插件 binding",
            )
        )
        return ()

    try:
        capability_definitions = _validated_capability_definitions(manifest, selected)
        if capability_definitions:
            checks.append(
                _check(
                    "capability_catalog",
                    "产品能力执行目录",
                    True,
                    f"cases={len(capability_definitions)}, static_verifiers=ready",
                )
            )
    except Exception as exc:  # noqa: BLE001 - a preflight error must be actionable
        error_code = str(getattr(exc, "code", type(exc).__name__))
        checks.append(
            _check(
                "capability_catalog",
                "产品能力执行目录",
                False,
                f"{error_code}: {exc}",
                "修复 Suite manifest、Case、fixture 或静态执行器/Verifier binding",
            )
        )
        return ()

    try:
        _private_runtime_configuration_snapshot(request.bot)
        checks.append(
            _check(
                "private_runtime_fingerprint",
                "私有运行配置指纹",
                True,
                "available",
            )
        )
    except Exception as exc:  # noqa: BLE001 - required configuration preflight
        checks.append(
            _check(
                "private_runtime_fingerprint",
                "私有运行配置指纹",
                False,
                str(exc),
                "配置稳定凭据，或移除无效的私有身份配置",
            )
        )
        return ()

    if request.dry_run:
        target = _make_target(
            target_id="dry-run",
            label="Dry Run",
            executor="dry_run",
            backend="none",
            model="",
            reasoning_effort="",
            config_fingerprint=_hash_json({"executor": "dry_run", "suite": request.suite}),
        )
        checks.append(_check("executor", "执行器", True, "dry_run"))
        return (target,)

    try:
        with _preserved_environment():
            runtime = load_evaluation_runtime(request.bot)
            config = load_config(env_prefix=runtime.spec.llm.env_prefix)
            case_checks = _suite_case_preflight(
                manifest=manifest,
                cases=selected,
                runtime=runtime,
                config=config,
                capability_definitions=capability_definitions,
            )
            checks.extend(case_checks)
            if any(not bool(item.get("ok")) for item in case_checks):
                return ()
            backend = str(getattr(runtime, "agent_backend", "native"))
            selected_drivers = {_case_plugin_driver(manifest, case)[1] for case in selected}
            needs_agent = bool(
                selected_drivers.intersection({"agent_configured", "agent_isolated"})
            )
            fingerprint_backend = "direct" if selected_drivers == {"direct_llm"} else backend
            config_fingerprint = _runtime_behavior_fingerprint(
                runtime,
                config,
                backend=fingerprint_backend,
            )
            if selected_drivers == {"direct_llm"}:
                target = _make_target(
                    target_id="chat-direct",
                    label="Chat LLM",
                    executor="direct_llm",
                    backend="direct",
                    model=str(config.llm.model or ""),
                    reasoning_effort="",
                    config_fingerprint=config_fingerprint,
                )
                credential_ready = bool(str(config.llm.api_key or "").strip())
                ready = bool(target.model) and credential_ready
                detail = (
                    f"executor=direct_llm, model={target.model or 'missing'}, "
                    f"credential={'configured' if credential_ready else 'missing'}"
                )
            elif needs_agent:
                model, effort = _configured_model(backend, config)
                target = _make_target(
                    target_id=f"{backend}-configured",
                    label=f"{backend.title()} configured",
                    executor="agent_configured",
                    backend=backend,
                    model=model,
                    reasoning_effort=effort,
                    config_fingerprint=config_fingerprint,
                )
                ready = backend in backend_ids() and bool(model)
                detail = f"executor=agent_configured, backend={backend}, model={model or 'missing'}"
                if backend == "codex":
                    command_ready = _codex_command_available(config, model, effort)
                    ready = ready and command_ready
                    detail += (
                        f", command={'available' if command_ready else 'missing'}"
                    )
                else:
                    credential_ready = bool(str(config.llm.api_key or "").strip())
                    ready = ready and credential_ready
                    detail += f", credential={'configured' if credential_ready else 'missing'}"
            else:
                selected_executor = next(iter(selected_drivers), "acp_scenario")
                executor: TargetExecutor = (
                    selected_executor
                    if selected_executor in {"acp_scenario", "qq_message_flow"}
                    else "acp_scenario"
                )  # type: ignore[assignment]
                target = _make_target(
                    target_id=f"{executor}-configured",
                    label=(
                        "QQ message flow"
                        if executor == "qq_message_flow"
                        else "ACP scenario"
                    ),
                    executor=executor,
                    backend=backend,
                    model="",
                    reasoning_effort="",
                    config_fingerprint=config_fingerprint,
                )
                ready = bool(selected_drivers) and selected_drivers in (
                    {"acp_scenario"},
                    {"qq_message_flow"},
                )
                detail = f"executor={executor}, drivers={','.join(sorted(selected_drivers))}"
            checks.append(
                _check(
                    "executor",
                    "执行器",
                    ready,
                    detail,
                    "检查 Bot LLM、backend 与凭据配置",
                )
            )
            return (target,)
    except Exception as exc:  # noqa: BLE001
        checks.append(_check("executor", "执行器", False, str(exc), "检查 BotSpec 与 local.env"))
        return ()


def _case_plugin_driver(manifest: Any, case: EvalCase) -> tuple[str, str]:
    metadata = case.metadata if isinstance(case.metadata, dict) else {}
    definition = metadata.get("case_definition")
    if not isinstance(definition, Mapping):
        definition = {}
    plugin_id = str(
        definition.get("plugin_id")
        or metadata.get("plugin")
        or metadata.get("plugin_id")
        or manifest.plugin_id
    ).strip()
    driver_id = str(
        definition.get("driver_id")
        or metadata.get("driver")
        or metadata.get("driver_id")
        or manifest.driver_id
    ).strip()
    if not plugin_id or not driver_id:
        raise ValueError(f"Case {case.case_id} has no trusted plugin/driver binding")
    return plugin_id, driver_id


def _case_definition(case: EvalCase) -> Mapping[str, Any]:
    metadata = case.metadata if isinstance(case.metadata, dict) else {}
    definition = metadata.get("case_definition")
    return definition if isinstance(definition, Mapping) else {}


def _validated_capability_definitions(
    manifest: Any,
    cases: Sequence[EvalCase],
) -> dict[str, Any]:
    """Load and validate product Case bindings before any runtime action.

    The packaged definition is the trusted source for the executor/verifier
    binding.  The selected ``EvalCase`` is a frozen projection of that
    definition, so both identities must agree before a dry run can claim that
    a product suite is runnable.
    """

    if str(getattr(manifest, "kind", "")) != "product":
        return {}
    definitions = {definition.case_id: definition for definition in load_case_definitions(manifest)}
    for case in cases:
        try:
            definition = definitions[case.case_id]
        except KeyError as exc:
            raise ValueError(
                f"selected product Case has no packaged definition: {case.case_id}"
            ) from exc
        validate_capability_definition(definition)
        case_plugin, case_driver = _case_plugin_driver(manifest, case)
        if (case_plugin, case_driver) != (definition.plugin_id, definition.driver_id):
            raise ValueError(
                "selected product Case plugin/driver differs from packaged definition: "
                f"{case.case_id}"
            )
    return definitions


def _expected_search_sources(definition: Any) -> frozenset[str]:
    """Read logical source requirements only from a validated Case definition."""

    sources: set[str] = set()
    for assertion in getattr(definition, "assertions", ()):
        arguments = getattr(assertion, "arguments", {})
        if not isinstance(arguments, Mapping):
            continue
        values = arguments.get("expected_source_hints")
        if not isinstance(values, (list, tuple)):
            continue
        sources.update(str(value).strip().lower() for value in values if str(value).strip())
    return frozenset(sources)


def _configured_search_sources(runtime: Any) -> frozenset[str]:
    """Return the logical search sources that this Bot can actually invoke.

    Direct providers provide the ``web`` source only when enabled and every
    declared credential is present.  Account-state/vertical sources are
    explicit trusted MCP bindings; their identifiers are deliberately mapped
    here rather than inferred from free-form Case data.
    """

    subagents = getattr(runtime, "subagents", None)
    if not bool(getattr(subagents, "research_enabled", False)):
        return frozenset()
    sources: set[str] = set()
    for provider in getattr(subagents, "search_providers", ()):
        if not bool(getattr(provider, "enabled", False)):
            continue
        kind = str(getattr(provider, "kind", "")).strip().lower()
        credential_env = str(getattr(provider, "credential_env", "") or "").strip()
        if credential_env and not str(os.environ.get(credential_env, "")).strip():
            continue
        if kind in {"tavily", "brave", "searxng"}:
            sources.add("web")

    # Only catalogued, search-only MCP server identities can satisfy a
    # non-web source.  This keeps a generic MCP with risk=search from silently
    # satisfying a product Case that explicitly demands experience evidence.
    mcp_source_by_id = {
        "xiaohongshu": "experience",
        "xiaohongshu-search": "experience",
        "github": "github",
        "github-readonly": "github",
        "taoke": "commerce",
    }
    for server in getattr(runtime, "mcp_servers", ()):
        if not bool(getattr(server, "enabled", False)):
            continue
        if str(getattr(server, "risk", "")).strip() != "search":
            continue
        if not tuple(getattr(server, "search_only_tools", ())):
            continue
        identifiers = (
            str(getattr(server, "id", "")).strip().lower(),
            str(getattr(server, "catalog_ref", "")).strip().lower(),
        )
        for identifier in identifiers:
            source = mcp_source_by_id.get(identifier)
            if source:
                sources.add(source)
    return frozenset(sources)


def _suite_case_preflight(
    *,
    manifest: Any,
    cases: Sequence[EvalCase],
    runtime: Any,
    config: Any,
    capability_definitions: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Validate selected product Case requirements without executing a Trial."""

    checks: list[dict[str, Any]] = []
    definitions = dict(capability_definitions or {})
    if str(getattr(manifest, "kind", "")) == "product" and not definitions:
        # Keep direct callers fail-closed while the public validation path
        # loads this once, before dry-run/external-write branching.
        definitions = _validated_capability_definitions(manifest, cases)
    backend = str(getattr(runtime, "agent_backend", "native"))
    platform = str(getattr(runtime, "platform_type", ""))
    features = set(str(value) for value in getattr(runtime, "tool_features", ()))
    # Case requirements use product-capability names while BotSpec keeps its
    # stable component-catalog identifiers.  This is a closed, Core-owned
    # projection rather than a free-form alias mechanism.
    available_features = {*features, "chat", "tool_visibility"}
    if "chat.image_inputs" in features:
        available_features.update({"image_input", "multiple_image_input"})
    if str(getattr(runtime, "memory_namespace", "")).strip():
        available_features.add("session_memory")
    if "persona.control" in tuple(getattr(runtime, "tool_packs", ()) or ()):
        available_features.add("persona_control")
    packs = set(str(value) for value in getattr(runtime, "tool_packs", ()))
    configured_tools: set[str] = set()
    try:
        configured_tools.update(
            tool.name
            for tool in discover_tools(
                tool_packs=runtime.tool_packs,
                exclude_tools=runtime.exclude_tools,
            )
        )
    except ToolMaterializationError as exc:
        checks.append(
            _check(
                "tool_materialization",
                "工具物化",
                False,
                str(exc),
                "修复显式启用的 tool pack 绑定或工具模块",
            )
        )
    configured_search_sources = _configured_search_sources(runtime)

    for case in cases:
        definition = _case_definition(case)
        requirements = definition.get("requirements")
        if not isinstance(requirements, Mapping):
            requirements = {}
        plugin_id, driver_id = _case_plugin_driver(manifest, case)
        missing: list[str] = []
        capability_definition = definitions.get(case.case_id)
        if definitions:
            if capability_definition is None:
                missing.append("executor:definition_missing")
        required_backends = set(str(value) for value in requirements.get("backends", ()))
        if required_backends and backend not in required_backends:
            missing.append("backend")
        required_platforms = set(str(value) for value in requirements.get("platforms", ()))
        available_platforms = {platform, "acp"}
        if not required_platforms.issubset(available_platforms):
            missing.extend(
                f"platform:{value}" for value in sorted(required_platforms - available_platforms)
            )
        required_features = set(str(value) for value in requirements.get("features", ()))
        case_features = set(available_features)
        if case.case_id == "subagent-structured-result":
            # This Case uses the Core-owned no-tool delegate registered by the
            # isolated capability executor, not a production Bot delegate.
            case_features.add("subagents")
        if not required_features.issubset(case_features):
            missing.extend(
                f"feature:{value}" for value in sorted(required_features - case_features)
            )
        required_packs = set(str(value) for value in requirements.get("tool_packs", ()))
        if driver_id == "agent_configured" and not required_packs.issubset(packs):
            missing.extend(f"tool_pack:{value}" for value in sorted(required_packs - packs))
        required_tools = set(str(value) for value in requirements.get("tools", ()))
        # Isolated drivers receive only Core-owned deterministic fixture tools.
        # Configured search Cases use the selected Bot's provider configuration,
        # but direct Codex receives it through the explicit eval-only bridge.
        if driver_id == "agent_configured":
            available_case_tools = set(configured_tools)
            expected_search_sources = _expected_search_sources(capability_definition)
            if expected_search_sources and expected_search_sources.issubset(
                configured_search_sources
            ):
                available_case_tools.add("search_information")
            if expected_search_sources:
                missing.extend(
                    f"search_source:{source}"
                    for source in sorted(expected_search_sources - configured_search_sources)
                )
            if not required_tools.issubset(available_case_tools):
                missing.extend(
                    f"tool:{value}" for value in sorted(required_tools - available_case_tools)
                )
        env_keys = tuple(str(value) for value in requirements.get("env_keys", ()))
        missing.extend(f"env:{key}" for key in env_keys if not str(os.environ.get(key, "")).strip())
        if case.case_id == "subagent-structured-result":
            if not str(getattr(config.llm, "model", "") or "").strip():
                missing.append("chat_llm_model")
            if not str(getattr(config.llm, "api_key", "") or "").strip():
                missing.append("chat_llm_credential")
        checks.append(
            _check(
                f"case_requirements:{case.case_id}",
                f"Case 配置 · {case.case_id}",
                not missing,
                (
                    f"plugin={plugin_id}, driver={driver_id}, requirements=ready"
                    if not missing
                    else "missing=" + ",".join(missing)
                ),
                "启用 Case 所需 backend、feature、tool pack、tool 或 env key",
            )
        )

    return checks


def _private_configuration_digest(value: str, *, fallback_secret: str = "") -> str:
    if not value:
        try:
            key = _private_configuration_key(fallback_secret=fallback_secret)
        except ValueError:
            return ""
    else:
        key = _private_configuration_key(fallback_secret=fallback_secret)
    if not key:
        if value:
            raise ValueError("stable private configuration digest key is unavailable")
        return ""
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).hexdigest()


def _execution_cases(
    request: EvaluationRequest,
) -> tuple[ProfileCase, ...] | tuple[EvalCase, ...]:
    if request.kind == "comparison":
        profile = get_profile(request.profile)
        known = {item.ref: item for item in profile.cases}
        return tuple(known[value] for value in request.case_refs)
    cases = get_cases(request.suite, auto_prepare=False)
    return _select_suite_cases(cases, request.case_ids)


def _trial_request(
    *,
    request: EvaluationRequest,
    output: Path,
    case: ProfileCase | EvalCase,
    target: EvaluationTarget,
    attempt: int,
    order: int,
    config_snapshot: Mapping[str, Any] | None = None,
) -> TrialExecutionRequest:
    if isinstance(case, ProfileCase):
        profile_case = case
        eval_case = case.case
        suite_id = case.suite_id
        dimension = case.dimension
        profile = request.profile if isinstance(request, ComparisonEvaluationRequest) else ""
    else:
        profile_case = None
        eval_case = case
        suite_id = request.suite if isinstance(request, SuiteEvaluationRequest) else ""
        dimension = case.category
        profile = ""
    plugin_id = ""
    driver_id = ""
    if isinstance(request, SuiteEvaluationRequest):
        manifest = get_manifest(request.suite)
        plugin_id, driver_id = _case_plugin_driver(manifest, eval_case)
        if request.dry_run:
            driver_id = "dry_run"
    frozen_definition_snapshot: dict[str, Any] = {}
    frozen_definition_fingerprint = ""
    frozen_environment_fingerprint = ""
    if isinstance(request, SuiteEvaluationRequest):
        snapshot = dict(config_snapshot or {})
        definition = snapshot.get("definition_snapshot")
        if not isinstance(definition, Mapping):
            raise ValueError("Suite Trial requires a frozen definition snapshot")
        # Serialize once at the parent boundary so mutable nested values cannot
        # be changed by later orchestration code before spawn pickles the Trial.
        frozen_definition_snapshot = json.loads(
            json.dumps(
                definition,
                allow_nan=False,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        frozen_definition_fingerprint = str(snapshot.get("definition_fingerprint") or "")
        frozen_environment_fingerprint = str(snapshot.get("environment_fingerprint") or "")
        if not frozen_definition_fingerprint or not frozen_environment_fingerprint:
            raise ValueError("Suite Trial requires frozen definition and environment fingerprints")
    return TrialExecutionRequest(
        evaluation_id=request.evaluation_id,
        kind=request.kind,
        bot=request.bot,
        output=output,
        suite_id=suite_id,
        profile=profile,
        profile_case=profile_case,
        case=eval_case,
        dimension=dimension,
        target=target,
        attempt=attempt,
        order=order,
        plugin_id=plugin_id,
        driver_id=driver_id,
        dry_run=isinstance(request, SuiteEvaluationRequest) and request.dry_run,
        llm_judge=isinstance(request, SuiteEvaluationRequest) and request.llm_judge,
        options=(dict(request.options) if isinstance(request, SuiteEvaluationRequest) else {}),
        confirm_external_write=(
            request.confirm_external_write if isinstance(request, SuiteEvaluationRequest) else False
        ),
        frozen_definition_snapshot=frozen_definition_snapshot,
        frozen_definition_fingerprint=frozen_definition_fingerprint,
        frozen_environment_fingerprint=frozen_environment_fingerprint,
    )


def _trial_from_case_result(
    request: TrialExecutionRequest,
    result: EvalCaseResult,
) -> EvaluationTrial:
    metadata = result.metadata if isinstance(result.metadata, dict) else {}
    usage = metadata.get("usage_totals")
    if not isinstance(usage, dict):
        usage = {}
    evidence = {key: value for key, value in metadata.items() if key != "usage_totals"}
    return EvaluationTrial(
        trial_id=_trial_id(request),
        evaluation_id=request.evaluation_id,
        kind=request.kind,
        bot=request.bot,
        profile=request.profile,
        suite_id=request.suite_id,
        case_ref=f"{request.suite_id}:{request.case.case_id}",
        case_id=request.case.case_id,
        dimension=request.dimension,
        target_id=request.target.target_id,
        target_fingerprint=request.target.fingerprint,
        executor=(request.driver_id or request.target.executor),  # type: ignore[arg-type]
        backend=request.target.backend,
        model=request.target.model,
        reasoning_effort=request.target.reasoning_effort,
        attempt=request.attempt,
        order=request.order,
        outcome=_normalize_outcome(result.status),
        score=result.score,
        max_score=result.max_score,
        passed=bool(result.judge and result.judge.passed),
        duration_seconds=result.duration_seconds,
        final_text=result.final_text,
        stop_reason=result.stop_reason,
        started_at=result.started_at,
        finished_at=result.finished_at,
        judge=to_jsonable(result.judge) if result.judge is not None else None,
        events=result.events,
        usage_totals={
            str(key): int(value) for key, value in usage.items() if isinstance(value, int)
        },
        evidence=evidence,
        error=result.error,
    )


def _build_result(
    *,
    request: EvaluationRequest,
    targets: tuple[EvaluationTarget, ...],
    cases: Sequence[ProfileCase | EvalCase],
    trials: Sequence[EvaluationTrial],
    status: EvaluationStatus,
    started_at: str,
    started_clock: float,
    previous_duration_seconds: float,
    config_snapshot: dict[str, Any],
    error: str = "",
    finished: bool = False,
) -> EvaluationResult:
    profile_value: str
    suite_value: str
    preset_value: str
    if request.kind == "comparison":
        profile_cases = tuple(case for case in cases if isinstance(case, ProfileCase))
        comparisons, dimensions, summary = aggregate_comparison(trials, profile_cases, targets)
        profile_value = request.profile
        suite_value = ""
        preset_value = request.preset
        repetitions = request.repetitions
        max_wall_seconds = request.max_wall_seconds
        seed = request.seed
    else:
        comparisons = ()
        dimensions = {}
        suite_cases = tuple(case for case in cases if isinstance(case, EvalCase))
        summary = _suite_summary(
            trials,
            cases=suite_cases,
            lifecycle_status=status,
            product_suite=get_manifest(request.suite).kind == "product",
            repetitions=request.repetitions,
        )
        profile_value = ""
        suite_value = request.suite
        preset_value = request.preset
        repetitions = request.repetitions
        max_wall_seconds = request.max_wall_seconds
        seed = request.seed
    return EvaluationResult(
        evaluation_id=request.evaluation_id,
        kind=request.kind,
        bot=request.bot,
        status=status,
        started_at=started_at,
        finished_at=_utc_now() if finished else "",
        duration_seconds=(previous_duration_seconds + time.monotonic() - started_clock),
        profile=profile_value,
        suite=suite_value,
        preset=preset_value,
        repetitions=repetitions,
        max_wall_seconds=max_wall_seconds,
        seed=seed,
        targets=targets,
        selected_cases=tuple(
            (case.ref if isinstance(case, ProfileCase) else f"{suite_value}:{case.case_id}")
            for case in cases
        ),
        trials=tuple(trials),
        comparisons=comparisons,
        dimensions=dimensions,
        summary=summary,
        config_snapshot=config_snapshot,
        error=sanitize_text(error, secrets=collect_env_secrets()),
    )


def _persist_evaluation(
    result: EvaluationResult,
    output: Path,
    *,
    write_state: bool = True,
) -> None:
    payload = _sanitize(evaluation_result_to_dict(result), output=output)
    if (output / "trials").is_symlink():
        raise ValueError("Evaluation trials root must not be a symlink")
    trial_root = contained_artifact_path(output, "trials")
    trial_artifacts = [
        (
            contained_artifact_path(
                output,
                "trials",
                f"{safe_artifact_component(trial.get('trial_id', ''))}.json",
            ),
            trial,
        )
        for trial in payload.get("trials", [])
    ]
    _write_json(output / "result.json", payload)
    state = {
        "evaluation_id": result.evaluation_id,
        "kind": result.kind,
        "status": result.status,
        "pid": None if result.status in _TERMINAL_STATUSES else os.getpid(),
        "started_at": result.started_at,
        "finished_at": result.finished_at,
        "duration_seconds": result.duration_seconds,
        "completed_trials": len(result.trials),
        "planned_trials": (len(result.selected_cases) * result.repetitions * len(result.targets)),
        "error": result.error,
    }
    if write_state:
        _write_json(output / "state.json", _sanitize(state, output=output))
    _write_text(output / "summary.md", _summary_markdown(payload))
    _ensure_private_dir(trial_root)
    for trial_path, trial in trial_artifacts:
        _write_json(trial_path, trial)


def _persist_request(
    request: EvaluationRequest,
    targets: Sequence[EvaluationTarget],
    output: Path,
) -> None:
    """Preserve application-owned descriptive metadata around the Core request."""

    path = output / "request.json"
    existing: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = loaded
        except (OSError, ValueError, json.JSONDecodeError):
            existing = {}
    bot_spec = existing.get("bot_spec") or _portable_bot_ref(request.bot)
    core_request = _runnable_request_dict(request)
    core_request["bot"] = bot_spec
    payload: dict[str, Any] = {
        **existing,
        "bot_id": existing.get("bot_id") or _bot_id(request.bot),
        "created_at": existing.get("created_at") or _utc_now(),
        "bot_spec": bot_spec,
        "evaluation_id": request.evaluation_id,
        "kind": request.kind,
        "targets": [to_jsonable(target) for target in targets],
        "core_request": core_request,
    }
    if request.kind == "comparison":
        payload.update(
            {
                "profile_id": request.profile,
                "preset": request.preset,
                "target_ids": list(request.targets),
                "case_refs": list(request.case_refs),
                "repetitions": request.repetitions,
                "max_wall_seconds": request.max_wall_seconds,
                "seed": request.seed,
            }
        )
    else:
        payload.update(
            {
                "suite_id": request.suite,
                "case_ids": list(request.case_ids),
                "preset": request.preset,
                "repetitions": request.repetitions,
                "max_wall_seconds": request.max_wall_seconds,
                "seed": request.seed,
                "options": dict(request.options),
                "confirm_external_write": request.confirm_external_write,
                "dry_run": request.dry_run,
                "llm_judge": request.llm_judge,
            }
        )
    _write_json(path, _sanitize(payload, output=output))


def _validate_fresh_output(
    *,
    output: Path,
    request: EvaluationRequest,
    targets: Sequence[EvaluationTarget],
    managed: bool = False,
) -> None:
    if not managed and is_managed_evaluation_output(output):
        raise ValueError("standalone Evaluation output cannot use the managed service root")
    if not output.exists():
        return
    if not output.is_dir():
        raise ValueError("Evaluation output already exists and is not a directory")
    entries = {entry.name: entry for entry in output.iterdir()}
    if managed:
        _validate_managed_bootstrap(
            entries=entries,
            request=request,
            targets=targets,
        )
        return
    if not entries:
        return
    raise ValueError("fresh standalone Evaluation output directory must be empty")


def _validate_managed_bootstrap(
    *,
    entries: Mapping[str, Path],
    request: EvaluationRequest,
    targets: Sequence[EvaluationTarget],
) -> None:
    required = {"request.json", "state.json"}
    allowed = {*required, "run.log", ".cancel-requested.json"}
    if not required.issubset(entries) or not set(entries).issubset(allowed):
        raise ValueError("Evaluation output is not a managed service bootstrap directory")
    for name, entry in entries.items():
        if entry.is_symlink() or not entry.is_file():
            raise ValueError(f"managed bootstrap artifact is unsafe: {name}")
    run_log = entries.get("run.log")
    if run_log is not None and run_log.stat().st_size:
        raise ValueError("managed bootstrap run.log must be empty before Core starts")
    stored_request = _read_private_json_object(
        entries["request.json"],
        "Evaluation service request.json",
    )
    expected = {
        **_expected_bootstrap_request(request, targets),
        "created_at": stored_request.get("created_at"),
        "core_request": _runnable_request_dict(request),
    }
    start_fingerprint = stored_request.get("start_request_fingerprint")
    if start_fingerprint is not None:
        if (
            not isinstance(start_fingerprint, str)
            or len(start_fingerprint) != 64
            or any(char not in "0123456789abcdef" for char in start_fingerprint)
        ):
            raise ValueError("Evaluation service start request fingerprint is invalid")
        expected["start_request_fingerprint"] = start_fingerprint
    bot_spec_digest = stored_request.get("bot_spec_sha256")
    if (
        not isinstance(bot_spec_digest, str)
        or len(bot_spec_digest) != 64
        or any(char not in "0123456789abcdef" for char in bot_spec_digest)
    ):
        raise ValueError("Evaluation service BotSpec snapshot digest is invalid")
    expected["bot_spec_sha256"] = bot_spec_digest
    if (
        not isinstance(stored_request.get("created_at"), str)
        or not str(stored_request["created_at"]).strip()
    ):
        raise ValueError("Evaluation service request created_at is invalid")
    if stored_request != expected:
        raise ValueError("Evaluation service request does not match managed worker")
    state = _read_private_json_object(
        entries["state.json"],
        "Evaluation service state.json",
    )
    expected_state_keys = {
        "evaluation_id",
        "kind",
        "status",
        "pid",
        "started_at",
        "finished_at",
        "duration_seconds",
        "completed_trials",
        "planned_trials",
        "error",
    }
    if set(state) != expected_state_keys:
        raise ValueError("Evaluation service state fields do not match managed worker")
    if (
        state.get("evaluation_id") != request.evaluation_id
        or state.get("kind") != request.kind
        or state.get("status") != "running"
        or isinstance(state.get("pid"), bool)
        or not isinstance(state.get("pid"), int)
        or state.get("pid") != os.getpid()
        or not isinstance(state.get("started_at"), str)
        or not str(state["started_at"]).strip()
        or state.get("finished_at") is not None
        or state.get("duration_seconds") is not None
        or state.get("completed_trials") != 0
        or state.get("error") is not None
        or isinstance(state.get("planned_trials"), bool)
        or not isinstance(state.get("planned_trials"), int)
        or state["planned_trials"] < 0
    ):
        raise ValueError("Evaluation service state does not match managed worker")


def _expected_bootstrap_request(
    request: EvaluationRequest,
    targets: Sequence[EvaluationTarget],
) -> dict[str, Any]:
    expected: dict[str, Any] = {
        "evaluation_id": request.evaluation_id,
        "kind": request.kind,
        "bot_id": _bot_id(request.bot),
        "bot_spec": _portable_bot_ref(request.bot),
        "targets": [to_jsonable(target) for target in targets],
    }
    if request.kind == "comparison":
        expected.update(
            {
                "profile_id": request.profile,
                "preset": request.preset,
                "target_ids": list(request.targets),
                "case_refs": list(request.case_refs),
                "repetitions": request.repetitions,
                "max_wall_seconds": request.max_wall_seconds,
                "seed": request.seed,
            }
        )
    else:
        expected.update(
            {
                "suite_id": request.suite,
                "case_ids": list(request.case_ids),
                "preset": request.preset,
                "repetitions": request.repetitions,
                "max_wall_seconds": request.max_wall_seconds,
                "seed": request.seed,
                "options": dict(request.options),
                "confirm_external_write": request.confirm_external_write,
                "dry_run": request.dry_run,
                "llm_judge": request.llm_judge,
            }
        )
    return expected


def _resume_checkpoint(
    *,
    output: Path,
    request: EvaluationRequest,
    targets: Sequence[EvaluationTarget],
    cases: Sequence[ProfileCase | EvalCase],
    config_snapshot: Mapping[str, Any],
) -> _ResumeCheckpoint:
    result_path = output / "result.json"
    _validate_resume_artifact_paths(output)
    if not result_path.is_file():
        raise ValueError("resume requested but result.json does not exist")
    payload = _read_json_object(result_path, "resume result.json")
    try:
        json.dumps(payload, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("resume result.json contains non-finite or invalid JSON values") from exc
    if payload.get("evaluation_id") != request.evaluation_id:
        raise ValueError("resume evaluation_id does not match result.json")
    if payload.get("kind") != request.kind:
        raise ValueError("resume kind does not match result.json")
    if payload.get("status") == "completed":
        raise ValueError("completed Evaluation cannot be resumed")
    if str(payload.get("error") or "").startswith(
        (_TRIAL_CLEANUP_ERROR_PREFIX, _ARTIFACT_INTEGRITY_ERROR_PREFIX)
    ):
        raise ValueError("quarantined Evaluation cannot be resumed")
    stored_snapshot = payload.get("config_snapshot")
    if not isinstance(stored_snapshot, Mapping):
        raise ValueError("resume result.json has no immutable config snapshot")
    identity_fields = [
        "request_hash",
        "case_hash",
        "target_fingerprints",
        "judge",
        "definition_fingerprint",
    ]
    if isinstance(request, SuiteEvaluationRequest):
        identity_fields.extend(
            [
                "private_runtime_configuration",
            ]
        )
    for field_name in identity_fields:
        if stored_snapshot.get(field_name) != config_snapshot.get(field_name):
            raise ValueError(f"resume {field_name} does not match result.json")
    trials = _validated_resume_trials(
        payload.get("trials"),
        request=request,
        targets=targets,
        cases=cases,
    )
    started_at = str(payload.get("started_at") or "").strip()
    if not started_at:
        raise ValueError("resume result.json has no started_at")
    duration_seconds = _finite_non_negative_number(
        payload.get("duration_seconds"),
        "resume duration_seconds",
    )
    return _ResumeCheckpoint(
        trials=trials,
        started_at=started_at,
        duration_seconds=duration_seconds,
    )


def _validate_resume_artifact_paths(output: Path) -> None:
    for name in (
        "request.json",
        "state.json",
        "result.json",
        "summary.md",
        "progress.jsonl",
        "trials",
        "workspaces",
    ):
        path = output / name
        if path.is_symlink():
            raise ValueError(f"resume artifact must not be a symlink: {name}")
        contained_artifact_path(output, name)


def _validated_resume_trials(
    raw_trials: Any,
    *,
    request: EvaluationRequest,
    targets: Sequence[EvaluationTarget],
    cases: Sequence[ProfileCase | EvalCase],
) -> tuple[EvaluationTrial, ...]:
    if not isinstance(raw_trials, list):
        raise ValueError("resume result.trials must be a list")
    target_by_id = {target.target_id: target for target in targets}
    expected_cases = {
        _case_ref(
            case,
            suite_id=request.suite if request.kind == "suite" else "",
        ): case
        for case in cases
    }
    expected_case_ids = {case_ref: case.case_id for case_ref, case in expected_cases.items()}
    repetitions = request.repetitions
    identities: set[tuple[str, int, str]] = set()
    trial_ids: set[str] = set()
    group_targets: dict[tuple[str, int], set[str]] = {}
    group_orders: dict[tuple[str, int], set[int]] = {}
    trials: list[EvaluationTrial] = []
    for index, raw in enumerate(raw_trials):
        if not isinstance(raw, Mapping):
            raise ValueError(f"resume Trial {index} must be an object")
        try:
            json.dumps(raw, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"resume Trial {index} contains non-finite or invalid JSON values"
            ) from exc
        events = raw.get("events")
        if not isinstance(events, list) or any(not isinstance(item, Mapping) for item in events):
            raise ValueError(f"resume Trial {index} events must be a list of objects")
        try:
            trial = _trial_from_dict(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"resume Trial {index} is malformed") from exc
        if trial.evaluation_id != request.evaluation_id or trial.kind != request.kind:
            raise ValueError(f"resume Trial {index} Evaluation identity does not match")
        expected_case_id = expected_case_ids.get(trial.case_ref)
        if expected_case_id is None or trial.case_id != expected_case_id:
            raise ValueError(f"resume Trial {index} Case identity does not match")
        if (
            isinstance(trial.attempt, bool)
            or not isinstance(trial.attempt, int)
            or not 1 <= trial.attempt <= repetitions
        ):
            raise ValueError(f"resume Trial {index} attempt is out of range")
        target = target_by_id.get(trial.target_id)
        if target is None or trial.target_fingerprint != target.fingerprint:
            raise ValueError(f"resume Trial {index} Target fingerprint does not match")
        expected_executor: str = target.executor
        if isinstance(request, SuiteEvaluationRequest):
            expected_case = expected_cases[trial.case_ref]
            if not isinstance(expected_case, EvalCase):
                raise ValueError(f"resume Trial {index} Case type does not match")
            expected_executor = (
                "dry_run"
                if request.dry_run
                else _case_plugin_driver(get_manifest(request.suite), expected_case)[1]
            )
        if (
            trial.executor != expected_executor
            or trial.backend != target.backend
            or trial.model != target.model
            or trial.reasoning_effort != target.reasoning_effort
        ):
            raise ValueError(f"resume Trial {index} Target semantics do not match")
        if (
            isinstance(trial.order, bool)
            or not isinstance(trial.order, int)
            or not 1 <= trial.order <= len(targets)
        ):
            raise ValueError(f"resume Trial {index} order is out of range")
        if trial.outcome not in {"passed", "failed", "skipped", "error"}:
            raise ValueError(f"resume Trial {index} outcome is invalid")
        expected_trial_id = trial_artifact_id(
            trial.case_id,
            attempt=trial.attempt,
            target_fingerprint=trial.target_fingerprint,
        )
        if trial.trial_id != expected_trial_id or trial.trial_id in trial_ids:
            raise ValueError(f"resume Trial {index} id is invalid or duplicated")
        identity = (trial.case_ref, trial.attempt, trial.target_id)
        if identity in identities:
            raise ValueError(f"resume Trial {index} identity is duplicated")
        identities.add(identity)
        trial_ids.add(trial.trial_id)
        group_key = (trial.case_ref, trial.attempt)
        group_targets.setdefault(group_key, set()).add(trial.target_id)
        orders = group_orders.setdefault(group_key, set())
        if trial.order in orders:
            raise ValueError(f"resume Trial {index} order is duplicated")
        orders.add(trial.order)
        trials.append(trial)

    expected_targets = set(target_by_id)
    if any(values != expected_targets for values in group_targets.values()):
        raise ValueError("resume result contains an incomplete Target group")
    return tuple(trials)


def _trial_from_dict(payload: Mapping[str, Any]) -> EvaluationTrial:
    values = dict(payload)
    values["events"] = tuple(item for item in values.get("events", []) if isinstance(item, dict))
    return EvaluationTrial(**values)


def _complete_group_keys(
    trials: Sequence[EvaluationTrial],
    targets: Sequence[EvaluationTarget],
) -> set[tuple[str, int]]:
    expected = {target.fingerprint for target in targets}
    grouped: dict[tuple[str, int], set[str]] = {}
    for trial in trials:
        grouped.setdefault((trial.case_ref, trial.attempt), set()).add(trial.target_fingerprint)
    return {key for key, fingerprints in grouped.items() if fingerprints == expected}


def _valid_complete_attempts(
    trials: Sequence[EvaluationTrial],
    targets: Sequence[EvaluationTarget],
) -> set[int]:
    attempts = {
        target.target_id: {
            trial.attempt
            for trial in trials
            if trial.target_id == target.target_id and trial.outcome in {"passed", "failed"}
        }
        for target in targets
    }
    return set.intersection(*attempts.values()) if attempts else set()


def _target_statistics(trials: Sequence[EvaluationTrial]) -> dict[str, Any]:
    valid = [trial for trial in trials if trial.outcome in {"passed", "failed"}]
    scores = [trial.score / trial.max_score if trial.max_score else 0.0 for trial in valid]
    usage: dict[str, int] = {}
    for trial in valid:
        for key, value in trial.usage_totals.items():
            usage[key] = usage.get(key, 0) + value
    return {
        "passed_count": sum(1 for trial in valid if trial.passed),
        "attempt_count": len(valid),
        "mean_score": (sum(scores) / len(scores)) if scores else None,
        "score_min": min(scores) if scores else None,
        "score_max": max(scores) if scores else None,
        "usage_totals": usage,
        "cost": "unknown",
    }


def _verdict(
    target_stats: Mapping[str, Mapping[str, Any]],
    target_ids: Sequence[str],
    has_samples: bool,
) -> str:
    if len(target_ids) != 2:
        return "not_applicable"
    if not has_samples:
        return "inconclusive"
    values = [target_stats[target_id].get("mean_score") for target_id in target_ids]
    if any(value is None for value in values):
        return "inconclusive"
    first_value, second_value = values
    if first_value is None or second_value is None:
        return "inconclusive"
    first, second = float(first_value), float(second_value)
    if abs(first - second) <= _TIE_THRESHOLD + 1e-12:
        return "tie"
    return target_ids[0] if first > second else target_ids[1]


def _suite_summary(
    trials: Sequence[EvaluationTrial],
    *,
    cases: Sequence[EvalCase] = (),
    lifecycle_status: EvaluationStatus = "completed",
    product_suite: bool = False,
    repetitions: int = 1,
) -> dict[str, Any]:
    outcomes = _outcome_counts(trials)
    score = sum(trial.score for trial in trials)
    max_score = sum(trial.max_score for trial in trials) or 1.0
    severity_by_case = {
        case.case_id: str(_case_definition(case).get("severity") or "required") for case in cases
    }
    critical_violations = sum(
        1
        for trial in trials
        if severity_by_case.get(trial.case_id) == "critical" and trial.outcome == "failed"
    )
    required_failures = sum(
        1
        for trial in trials
        if severity_by_case.get(trial.case_id, "required") in {"required", "critical"}
        and trial.outcome == "failed"
    )
    infrastructure_errors = outcomes["error"]
    verdict = "not_applicable"
    if product_suite:
        if lifecycle_status in {"queued", "running"}:
            verdict = "in_progress"
        elif lifecycle_status == "cancelled":
            verdict = "cancelled"
        elif lifecycle_status in {"error", "interrupted", "partial"} or infrastructure_errors:
            verdict = "error/indeterminate"
        elif outcomes["skipped"]:
            verdict = "error/indeterminate"
        elif required_failures or critical_violations:
            verdict = "failed"
        elif trials:
            verdict = "passed"
        else:
            verdict = "error/indeterminate"
    capability_groups: dict[str, dict[str, int]] = {}
    category_by_case = {case.case_id: case.category for case in cases}
    for trial in trials:
        category = category_by_case.get(trial.case_id, trial.dimension or "unknown")
        group = capability_groups.setdefault(
            category,
            {"total": 0, "passed": 0, "failed": 0, "skipped": 0, "errors": 0},
        )
        group["total"] += 1
        outcome_key = "errors" if trial.outcome == "error" else trial.outcome
        group[outcome_key] += 1
    summary = {
        "total": len(trials),
        "passed": outcomes["passed"],
        "failed": outcomes["failed"],
        "skipped": outcomes["skipped"],
        "errors": outcomes["error"],
        "outcomes": outcomes,
        "verdict": verdict,
        "critical_violations": critical_violations,
        "infrastructure_errors": infrastructure_errors,
        "capabilities": capability_groups,
        "reliability_note": (
            "Each Case ran once; this report does not measure repeated reliability."
            if repetitions == 1
            else f"Each selected Case was configured for {repetitions} repetitions."
        ),
        "score_scope": (
            "product capability gates are not averaged into an intelligence score"
            if product_suite
            else "official suite metric; not an AgentStrata product capability gate"
        ),
    }
    if not product_suite:
        summary.update(
            {
                "score": score,
                "max_score": max_score,
                "score_ratio": score / max_score,
            }
        )
    return summary


def _outcome_counts(trials: Sequence[EvaluationTrial]) -> dict[str, int]:
    return {
        outcome: sum(1 for trial in trials if trial.outcome == outcome)
        for outcome in ("passed", "failed", "skipped", "error")
    }


def _isolated_target(
    target_id: str,
    config: Any,
    *,
    config_fingerprint: str,
) -> tuple[EvaluationTarget, dict[str, Any]]:
    normalized = str(target_id).strip().lower()
    if normalized == "codex":
        model = str(config.routing.code_model or "")
        effort = str(config.routing.code_reasoning_effort or "")
        command_ready = _codex_command_available(config, model, effort)
        ready = bool(model and command_ready)
        detail = (
            f"backend=codex, model={model or 'missing'}, "
            f"command={'available' if command_ready else 'missing'}"
        )
        target = _make_target(
            target_id="codex",
            label="Codex",
            executor="agent_isolated",
            backend="codex",
            model=model,
            reasoning_effort=effort,
            config_fingerprint=config_fingerprint,
        )
    elif normalized == "native":
        model = str(config.llm.model or "")
        credential_ready = bool(str(config.llm.api_key or "").strip())
        ready = bool(model) and credential_ready
        detail = (
            f"backend=native, model={model or 'missing'}, "
            f"credential={'configured' if credential_ready else 'missing'}"
        )
        target = _make_target(
            target_id="native",
            label="Native Agent",
            executor="agent_isolated",
            backend="native",
            model=model,
            reasoning_effort="",
            config_fingerprint=config_fingerprint,
        )
    else:
        raise ValueError(f"unsupported comparison Target: {target_id}")
    return target, _check(
        f"target:{target.target_id}",
        f"Target · {target.label}",
        ready,
        detail,
        "检查 LLM、凭据和 Codex CLI 配置",
    )


def _codex_command_available(config: Any, model: str, effort: str) -> bool:
    try:
        command = build_codex_command(
            str(config.routing.code_command or ""),
            model=model,
            workdir=Path.cwd(),
            reasoning_effort=effort,
        )
        executable = Path(command[0])
        return executable.is_file() and os.access(executable, os.X_OK)
    except (IndexError, KeyError, OSError, RuntimeError, ValueError):
        return False


def _configured_model(backend: str, config: Any) -> tuple[str, str]:
    if backend == "codex":
        return (
            str(config.routing.code_model or ""),
            str(config.routing.code_reasoning_effort or ""),
        )
    return str(config.llm.model or ""), ""


_RUNTIME_CONFIG_FINGERPRINT_FIELDS = (
    "default_auto_mode",
    "max_tool_retries",
    "stream",
    "max_context_tokens",
    "sliding_window_turns",
    "tool_result_summary_max_tokens",
    "max_tool_iterations",
    "hard_iteration_cap",
    "max_tool_calls",
    "turn_timeout_seconds",
    "hard_timeout_seconds",
    "stall_window_seconds",
    "topic_classifier_enabled",
    "topic_classifier_mode",
    "topic_model",
    "topic_uncertain_mode",
    "topic_related_threshold",
    "topic_unrelated_threshold",
    "topic_decision_cache_size",
    "topic_decision_cache_ttl_seconds",
    "topic_current_max_chars",
    "topic_previous_user_max_chars",
    "topic_previous_assistant_max_chars",
)
_ROUTING_CONFIG_FINGERPRINT_FIELDS = (
    "enabled",
    "mode",
    "default_route",
    "code_prefixes",
    "chat_prefixes",
    "research_execution",
    "research_prefixes",
    "research_web_search",
    "code_provider",
    "code_model",
    "code_reasoning_effort",
    "code_profiles",
    "code_task_profile",
    "code_command",
    "code_workdir_env",
    "code_timeout_seconds",
    "code_allowed_roles",
)


def _resolved_chat_config_snapshot(config: ChatConfig) -> dict[str, Any]:
    """Project resolved ChatConfig behavior without endpoint or credential values."""

    base_url = str(getattr(config.llm, "base_url", "") or "")
    code_command = str(getattr(config.routing, "code_command", "") or "")
    routing = {
        field_name: _behavior_json_value(getattr(config.routing, field_name))
        for field_name in _ROUTING_CONFIG_FINGERPRINT_FIELDS
        if field_name != "code_command"
    }
    routing["code_command_sha256"] = (
        hashlib.sha256(code_command.encode("utf-8")).hexdigest() if code_command else ""
    )
    return {
        "schema": "agentstrata-resolved-chat-config/v1",
        "llm": {
            "base_url_sha256": (
                hashlib.sha256(base_url.encode("utf-8")).hexdigest() if base_url else ""
            ),
            "model": str(getattr(config.llm, "model", "") or ""),
            "timeout": getattr(config.llm, "timeout", None),
        },
        "runtime": {
            field_name: _behavior_json_value(getattr(config.runtime, field_name))
            for field_name in _RUNTIME_CONFIG_FINGERPRINT_FIELDS
        },
        "routing": routing,
    }


def _runtime_behavior_fingerprint(
    runtime: Any,
    config: ChatConfig,
    *,
    backend: str | None = None,
) -> str:
    """Hash resolved, non-secret behavior that can affect an Evaluation Target."""

    effective_backend = str(backend or runtime.agent_backend).strip().lower()

    mcp_servers: list[dict[str, Any]] = []
    for server in runtime.mcp_servers:
        value = to_jsonable(server)
        env = value.pop("env", {})
        headers = value.pop("headers", {})
        value["env_keys"] = sorted(str(key) for key in env)
        value["header_keys"] = sorted(str(key) for key in headers)
        mcp_servers.append(value)
    rag_sources = [
        {
            "path": _portable_behavior_path(source.path, runtime.source_path.parent),
            "label": source.label,
            "include": list(source.include),
            "exclude": list(source.exclude),
            "max_chunk_chars": source.max_chunk_chars,
        }
        for source in runtime.rag_sources
    ]
    skills = [
        {
            "id": entry.id,
            "name": entry.name,
            "description": entry.description,
            "body_hash": hashlib.sha256(entry.body_path.read_bytes()).hexdigest(),
        }
        for entry in runtime.skills
    ]
    payload = {
        "bot_spec_hash": _hash_json(_behavior_json_value(runtime.spec.raw)),
        # Keep the deliberately whitelisted resolved configuration opaque to
        # the generic path/secret redactor so slash-prefixed routing commands
        # cannot collapse to the same redacted value.
        "resolved_chat_config_sha256": _hash_json(_resolved_chat_config_snapshot(config)),
        "prompts": {
            "schema_version": 2,
            "identity": runtime.prompt_profile.identity,
            "response_style": runtime.prompt_profile.response_style,
            "refusal_style": runtime.prompt_profile.refusal_style,
            "mode_styles": runtime.prompt_profile.mode_styles,
            "role_styles": runtime.prompt_profile.role_styles,
            "capability_policies": list(runtime.capability_policies),
        },
        "tools": {
            "packs": list(runtime.tool_packs),
            "features": list(runtime.tool_features),
            "exclude": list(runtime.exclude_tools),
            "mcp_servers": mcp_servers,
        },
        "agent": {
            "backend": effective_backend,
            "subagents": _behavior_json_value(to_jsonable(runtime.subagents)),
        },
        "runtime_implementation": runtime_implementation_snapshot(effective_backend),
        "context": {
            "spec": _behavior_json_value(to_jsonable(runtime.spec.context)),
            "memory_namespace": runtime.memory_namespace,
            "rag_sources": rag_sources,
            "skills": skills,
        },
        "access": _behavior_json_value(to_jsonable(runtime.access)),
    }
    sanitized = redact_payload(
        _behavior_json_value(payload),
        secrets=collect_env_secrets(),
        roots={
            "repository": Path.cwd(),
            "bot": runtime.source_path.parent,
        },
    )
    return _hash_json(sanitized)


def _portable_behavior_path(path: Path, bot_root: Path) -> str:
    resolved = path.expanduser().resolve()
    try:
        return "$BOT/" + resolved.relative_to(bot_root.resolve()).as_posix()
    except ValueError:
        return "external:" + hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()


def _behavior_json_value(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _behavior_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_behavior_json_value(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _make_target(
    *,
    target_id: str,
    label: str,
    executor: TargetExecutor,
    backend: str,
    model: str,
    reasoning_effort: str,
    config_fingerprint: str,
) -> EvaluationTarget:
    fingerprint_payload = {
        "target_id": target_id,
        "executor": executor,
        "backend": backend,
        "model": model,
        "reasoning_effort": reasoning_effort,
        "config_fingerprint": config_fingerprint,
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            fingerprint_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return EvaluationTarget(
        target_id=target_id,
        label=label,
        executor=executor,
        backend=backend,
        model=model,
        reasoning_effort=reasoning_effort,
        fingerprint=fingerprint,
        config_fingerprint=config_fingerprint,
    )


def _target_from_dict(payload: Mapping[str, Any]) -> EvaluationTarget:
    return EvaluationTarget(**dict(payload))


def _config_snapshot(
    request: EvaluationRequest,
    targets: Sequence[EvaluationTarget],
    cases: Sequence[ProfileCase | EvalCase],
) -> dict[str, Any]:
    case_payload = [
        to_jsonable(case.case if isinstance(case, ProfileCase) else case) for case in cases
    ]
    snapshot: dict[str, Any] = {
        "request_hash": _hash_json(_effective_request_dict(request)),
        "case_hash": _hash_json(case_payload),
        "target_fingerprints": {target.target_id: target.fingerprint for target in targets},
        "judge": (
            "gaia-llm-fallback"
            if isinstance(request, SuiteEvaluationRequest) and request.llm_judge
            else "suite-or-profile-defined"
        ),
    }
    if isinstance(request, SuiteEvaluationRequest):
        manifest = get_manifest(request.suite)
        suite_cases = tuple(case for case in cases if isinstance(case, EvalCase))
        target_material = {target.target_id: target.fingerprint for target in targets}
        plugin = get_evaluation_plugin(manifest.plugin_id)
        definition = suite_definition_snapshot(
            manifest,
            plugin,
            suite_cases,
            target_fingerprint=target_material,
        )
        # Hash the exact snapshot already inspected above. Re-reading source
        # modules here could mix two generations if code changes mid-snapshot.
        definition["base_fingerprint"] = _hash_json(definition)
        private_runtime_configuration = _private_runtime_configuration_snapshot(request.bot)
        environment_fingerprint = _hash_json(private_runtime_configuration)
        definition["environment_identity"] = {
            "private_runtime_configuration_sha256": environment_fingerprint,
        }
        snapshot.update(
            {
                "definition_snapshot": definition,
                "definition_fingerprint": _hash_json(definition),
                "environment_fingerprint": environment_fingerprint,
                "private_runtime_configuration": private_runtime_configuration,
                "model_version_note": (
                    "Configured model identity is recorded; provider-side immutable version "
                    "was not reported during preflight, so model-side drift remains possible."
                ),
            }
        )
    else:
        implementations = comparison_implementation_snapshot()
        definition = {
            "schema": "agentstrata-comparison-definition/v1",
            "profile": request.profile,
            "execution_implementations": implementations,
        }
        snapshot["definition_snapshot"] = definition
        snapshot["definition_fingerprint"] = _hash_json(definition)
        snapshot["execution_implementations"] = implementations
        # Comparison resume/compare already require case_hash equality. Bind
        # the exact isolated executor, profile loader and scorer sources into
        # that existing immutable identity instead of adding a parallel gate.
        snapshot["case_hash"] = _hash_json(
            {
                "cases": case_payload,
                "execution_implementations": implementations,
            }
        )
    return snapshot


def _private_runtime_configuration_snapshot(
    bot: str,
) -> dict[str, Any]:
    """Return presence/count/digest evidence without persisting identities."""

    if not bot:
        return {}
    with _preserved_environment():
        runtime = load_evaluation_runtime(bot)
        config = load_config(env_prefix=runtime.spec.llm.env_prefix)
        user_allowlist = parse_numeric_allowlist(
            os.environ.get("QQ_ALLOW_FROM"),
            field="QQ_ALLOW_FROM",
        )
        group_allowlist = parse_numeric_allowlist(
            os.environ.get("QQ_ALLOW_GROUPS"),
            field="QQ_ALLOW_GROUPS",
        )
        owners = tuple(
            value.strip()
            for value in str(os.environ.get("CHATCOPILOT_ADD_OWNER_IDS", "")).split(",")
            if value.strip()
        )
        admins = tuple(
            value.strip()
            for value in str(os.environ.get("CHATCOPILOT_ADD_ADMIN_IDS", "")).split(",")
            if value.strip()
        )
        user_mode = "all" if user_allowlist.allow_all else (
            "finite" if user_allowlist.values else "empty"
        )
        group_mode = "all" if group_allowlist.allow_all else (
            "finite" if group_allowlist.values else "empty"
        )
        has_private_identities = bool(
            user_allowlist.values or group_allowlist.values or owners or admins
        )
        material = (
            "\0".join(
                (
                    f"users:{user_mode}",
                    *sorted(user_allowlist.values),
                    f"groups:{group_mode}",
                    *sorted(group_allowlist.values),
                    "owners",
                    *owners,
                    "admins",
                    *admins,
                )
            )
            if has_private_identities
            else ""
        )
        fallback_secret = str(getattr(config.llm, "api_key", "") or "")
        snapshot: dict[str, Any] = {
            "qq_user_allowlist_mode": user_mode,
            "qq_user_allowlist_entry_count": len(user_allowlist.values),
            "qq_group_allowlist_mode": group_mode,
            "qq_group_allowlist_entry_count": len(group_allowlist.values),
            "owner_entry_count": len(owners),
            "admin_entry_count": len(admins),
            "identity_hmac": (
                _private_configuration_digest(
                    material,
                    fallback_secret=fallback_secret,
                )
                if has_private_identities
                else ""
            ),
        }
        return snapshot


def _private_configuration_key(*, fallback_secret: str = "") -> bytes:
    secrets = sorted(secret for secret in collect_env_secrets() if secret)
    key_material = "\0".join(secrets) or fallback_secret
    if not key_material:
        raise ValueError("stable private configuration digest key is unavailable")
    return hashlib.sha256(key_material.encode("utf-8")).digest()


def _effective_request_dict(request: EvaluationRequest) -> dict[str, Any]:
    payload = to_jsonable(request)
    payload["bot"] = _portable_bot_ref(request.bot)
    return payload


def _runnable_request_dict(request: EvaluationRequest) -> dict[str, Any]:
    if request.kind == "comparison":
        payload: dict[str, Any] = {
            "evaluation_id": request.evaluation_id,
            "kind": request.kind,
            "bot": request.bot,
            "profile": request.profile,
            "preset": request.preset,
        }
        if request.preset == "custom":
            payload.update(
                {
                    "targets": list(request.targets),
                    "case_refs": list(request.case_refs),
                    "repetitions": request.repetitions,
                    "max_wall_seconds": request.max_wall_seconds,
                    "seed": request.seed,
                }
            )
        return payload
    return {
        "evaluation_id": request.evaluation_id,
        "kind": request.kind,
        "bot": request.bot,
        "suite": request.suite,
        "case_ids": (
            list(request.case_ids)
            if request.preset in {"", "custom"}
            else []
        ),
        "preset": request.preset,
        "repetitions": request.repetitions,
        "max_wall_seconds": request.max_wall_seconds,
        "seed": request.seed,
        "options": dict(request.options),
        "confirm_external_write": request.confirm_external_write,
        "dry_run": request.dry_run,
        "llm_judge": request.llm_judge,
    }


def _validation_payload(
    ready: bool,
    checks: Sequence[Mapping[str, Any]],
    request: EvaluationRequest | None,
    targets: Sequence[EvaluationTarget],
) -> dict[str, Any]:
    failed_codes = {str(item.get("code") or "") for item in checks if not bool(item.get("ok"))}
    configuration_failure = any(
        code.startswith(("case_requirements:", "access_"))
        or code in {"external_write_confirmation", "private_runtime_fingerprint"}
        for code in failed_codes
    )
    payload = {
        "ready": bool(ready),
        "code": (
            ""
            if ready
            else "configuration_invalid"
            if configuration_failure
            else "preflight_failed"
        ),
        "message": (
            ""
            if ready
            else "评测配置无效，未创建 Evaluation"
            if configuration_failure
            else "评测预检未通过，未创建 Evaluation"
        ),
        "checks": [dict(item) for item in checks],
        "effective_request": (_effective_request_dict(request) if request is not None else None),
        "targets": [to_jsonable(target) for target in targets],
    }
    return redact_payload(
        payload,
        secrets=collect_env_secrets(),
        roots={"repository": Path.cwd()},
    )


def _select_suite_cases(
    cases: Sequence[EvalCase],
    case_ids: Sequence[str],
) -> tuple[EvalCase, ...]:
    if not case_ids:
        return tuple(cases)
    known = {case.case_id: case for case in cases}
    missing = [case_id for case_id in case_ids if case_id not in known]
    if missing:
        raise ValueError(f"unknown Suite Case ids: {', '.join(missing)}")
    return tuple(known[case_id] for case_id in case_ids)


def _case_ref(
    case: ProfileCase | EvalCase,
    *,
    suite_id: str = "",
) -> str:
    if isinstance(case, ProfileCase):
        return case.ref
    if suite_id:
        return f"{suite_id}:{case.case_id}"
    suite_id = str(case.metadata.get("suite_id", "")).strip()
    return f"{suite_id}:{case.case_id}" if suite_id else case.case_id


def _trial_id(request: TrialExecutionRequest) -> str:
    return trial_artifact_id(
        request.case.case_id,
        attempt=request.attempt,
        target_fingerprint=request.target.fingerprint,
    )


def _reset_trial_workspace(request: TrialExecutionRequest) -> None:
    output = request.output.expanduser().resolve()
    workspace_link = output / "workspaces"
    if workspace_link.is_symlink():
        raise ValueError("Evaluation workspace root must not be a symlink")
    workspace_root = contained_artifact_path(output, "workspaces")
    if workspace_root.exists() and not workspace_root.is_dir():
        raise ValueError("Evaluation workspace root must be a directory")
    _ensure_private_dir(workspace_root)
    candidate_link = workspace_root / _trial_id(request)
    if candidate_link.is_symlink():
        raise ValueError("Evaluation Trial workspace must not be a symlink")
    candidate = contained_artifact_path(
        output,
        "workspaces",
        _trial_id(request),
    )
    if candidate.exists():
        if not candidate.is_dir():
            raise ValueError("Evaluation Trial workspace must be a directory")
        shutil.rmtree(candidate)
    _ensure_private_dir(candidate)


def _discard_trial_workspace(request: TrialExecutionRequest) -> None:
    """Remove evidence from an interrupted, non-checkpointed target group."""

    output = request.output.expanduser().resolve()
    workspace_root = contained_artifact_path(output, "workspaces")
    if workspace_root.is_symlink():
        raise ValueError("Evaluation workspace root must not be a symlink")
    candidate_link = workspace_root / _trial_id(request)
    if candidate_link.is_symlink():
        raise ValueError("Evaluation Trial workspace must not be a symlink")
    candidate = contained_artifact_path(
        output,
        "workspaces",
        _trial_id(request),
    )
    if not candidate.exists():
        return
    if not candidate.is_dir():
        raise ValueError("Evaluation Trial workspace must be a directory")
    shutil.rmtree(candidate)


def _normalize_outcome(value: Any) -> TrialOutcome:
    normalized = str(value).strip().lower()
    if normalized in {"passed", "failed", "skipped", "error"}:
        return normalized  # type: ignore[return-value]
    return "error"


def _error_trial(
    request: TrialExecutionRequest,
    exc: Exception,
) -> EvaluationTrial:
    now = _utc_now()
    return EvaluationTrial(
        trial_id=_trial_id(request),
        evaluation_id=request.evaluation_id,
        kind=request.kind,
        bot=request.bot,
        profile=request.profile,
        suite_id=request.suite_id,
        case_ref=(
            request.profile_case.ref
            if request.profile_case is not None
            else f"{request.suite_id}:{request.case.case_id}"
        ),
        case_id=request.case.case_id,
        dimension=request.dimension,
        target_id=request.target.target_id,
        target_fingerprint=request.target.fingerprint,
        executor=(request.driver_id or request.target.executor),  # type: ignore[arg-type]
        backend=request.target.backend,
        model=request.target.model,
        reasoning_effort=request.target.reasoning_effort,
        attempt=request.attempt,
        order=request.order,
        outcome="error",
        started_at=now,
        finished_at=now,
        error=f"{type(exc).__name__}: {exc}",
    )


def _sanitize_trial(trial: EvaluationTrial, *, output: Path) -> EvaluationTrial:
    _assert_bounded_trial(trial)
    payload = _sanitize(to_jsonable(trial), output=output)
    try:
        encoded = json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("Trial is not finite canonical JSON") from exc
    if len(encoded) > _MAX_TRIAL_ARTIFACT_BYTES:
        raise ValueError(f"Trial exceeds {_MAX_TRIAL_ARTIFACT_BYTES} persisted bytes")
    payload["events"] = tuple(payload.get("events", []))
    return EvaluationTrial(**payload)


def _assert_bounded_trial(trial: EvaluationTrial) -> None:
    if len(trial.events) > _MAX_TRIAL_EVENTS:
        raise ValueError(f"Trial contains more than {_MAX_TRIAL_EVENTS} events")
    values = (
        trial.judge,
        trial.events,
        trial.usage_totals,
        trial.tool_summary,
        trial.evidence,
    )
    budget = [_MAX_TRIAL_JSON_NODES]
    active: set[int] = set()
    for value in values:
        _assert_bounded_json_value(value, depth=0, budget=budget, active=active)
    for text_value in (
        trial.trial_id,
        trial.evaluation_id,
        trial.bot,
        trial.profile,
        trial.suite_id,
        trial.case_ref,
        trial.case_id,
        trial.dimension,
        trial.target_id,
        trial.target_fingerprint,
        trial.backend,
        trial.model,
        trial.reasoning_effort,
        trial.final_text,
        trial.stop_reason,
        trial.started_at,
        trial.finished_at,
        trial.error,
    ):
        if len(text_value) > _MAX_TRIAL_STRING_CHARS:
            raise ValueError("Trial contains an oversized text field")


def _assert_bounded_json_value(
    value: Any,
    *,
    depth: int,
    budget: list[int],
    active: set[int],
) -> None:
    budget[0] -= 1
    if budget[0] < 0:
        raise ValueError(f"Trial contains more than {_MAX_TRIAL_JSON_NODES} JSON nodes")
    if depth > _MAX_TRIAL_JSON_DEPTH:
        raise ValueError(f"Trial JSON nesting exceeds {_MAX_TRIAL_JSON_DEPTH}")
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Trial contains NaN or Infinity")
        return
    if isinstance(value, str):
        if len(value) > _MAX_TRIAL_STRING_CHARS:
            raise ValueError("Trial contains an oversized string")
        return
    if isinstance(value, Mapping):
        if len(value) > _MAX_TRIAL_COLLECTION_ITEMS:
            raise ValueError("Trial contains an oversized mapping")
        identity = id(value)
        if identity in active:
            raise ValueError("Trial contains a recursive mapping")
        active.add(identity)
        try:
            for key, item in value.items():
                if not isinstance(key, str) or len(key) > _MAX_TRIAL_KEY_CHARS:
                    raise ValueError("Trial contains an invalid mapping key")
                _assert_bounded_json_value(
                    item,
                    depth=depth + 1,
                    budget=budget,
                    active=active,
                )
        finally:
            active.remove(identity)
        return
    if isinstance(value, (list, tuple)):
        if len(value) > _MAX_TRIAL_COLLECTION_ITEMS:
            raise ValueError("Trial contains an oversized collection")
        identity = id(value)
        if identity in active:
            raise ValueError("Trial contains a recursive collection")
        active.add(identity)
        try:
            for item in value:
                _assert_bounded_json_value(
                    item,
                    depth=depth + 1,
                    budget=budget,
                    active=active,
                )
        finally:
            active.remove(identity)
        return
    raise ValueError(f"Trial contains a non-JSON value: {type(value).__name__}")


def _sanitize(value: Any, *, output: Path) -> Any:
    return redact_payload(
        value,
        secrets=collect_env_secrets(),
        roots={"evaluation": output, "repository": Path.cwd()},
    )


def _summary_markdown(payload: Mapping[str, Any]) -> str:
    summary = payload.get("summary", {})
    lines = [
        f"# Evaluation · {payload.get('evaluation_id', '')}",
        "",
        f"- kind: `{payload.get('kind', '')}`",
        f"- status: `{payload.get('status', '')}`",
        f"- bot: `{payload.get('bot', '')}`",
        f"- profile: `{payload.get('profile', '')}`",
        f"- suite: `{payload.get('suite', '')}`",
        f"- preset: `{payload.get('preset', '')}`",
        f"- repetitions: `{payload.get('repetitions', 1)}`",
        f"- trials: `{summary.get('trial_count', summary.get('total', 0))}`",
        "",
    ]
    if payload.get("kind") == "comparison":
        lines.extend(
            [
                "## Capability dimensions",
                "",
                "| Dimension | Cases | Paired samples | Verdict |",
                "| --- | ---: | ---: | --- |",
            ]
        )
        for dimension, item in payload.get("dimensions", {}).items():
            lines.append(
                f"| {dimension} | {item.get('case_count', 0)} | "
                f"{item.get('sample_size', 0)} | {item.get('verdict', '')} |"
            )
    else:
        lines.extend(
            [
                "## Outcomes",
                "",
                f"- verdict: `{summary.get('verdict', 'not_applicable')}`",
                f"- passed: `{summary.get('passed', 0)}`",
                f"- failed: `{summary.get('failed', 0)}`",
                f"- skipped: `{summary.get('skipped', 0)}`",
                f"- errors: `{summary.get('errors', 0)}`",
                f"- critical violations: `{summary.get('critical_violations', 0)}`",
                f"- infrastructure errors: `{summary.get('infrastructure_errors', 0)}`",
                f"- reliability: {summary.get('reliability_note', '')}",
            ]
        )
        if summary.get("verdict") == "not_applicable":
            lines.append(f"- official score_ratio: `{float(summary.get('score_ratio', 0)):.3f}`")
        capabilities = summary.get("capabilities")
        if isinstance(capabilities, Mapping) and capabilities:
            lines.extend(
                [
                    "",
                    "## Capability families",
                    "",
                    "| Capability | Trials | Passed | Failed | Errors | Skipped |",
                    "| --- | ---: | ---: | ---: | ---: | ---: |",
                ]
            )
            for capability, raw in capabilities.items():
                item = raw if isinstance(raw, Mapping) else {}
                lines.append(
                    f"| {capability} | {item.get('total', 0)} | "
                    f"{item.get('passed', 0)} | {item.get('failed', 0)} | "
                    f"{item.get('errors', 0)} | {item.get('skipped', 0)} |"
                )
    lines.append("")
    return "\n".join(lines)


@contextmanager
def _preserved_environment() -> Iterator[None]:
    before = dict(os.environ)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(before)


def _reject_extra_fields(request: Mapping[str, Any], allowed: set[str]) -> None:
    extras = sorted(str(key) for key in request if str(key) not in allowed)
    if extras:
        raise ValueError(f"unexpected fields: {', '.join(extras)}")


def _evaluation_id(value: Any) -> str:
    identifier = str(value or "").strip() or _new_evaluation_id()
    if not _ID_PATTERN.fullmatch(identifier):
        raise ValueError("evaluation_id must use 1-128 letters, digits, '_' or '-'")
    return identifier


def _required_text(value: Any, field_name: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ValueError(f"{field_name} is required")
    return result


def _unique_texts(value: Any, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{field_name} must be a non-empty list")
    result = tuple(str(item).strip() for item in value)
    if not result or any(not item for item in result):
        raise ValueError(f"{field_name} must be a non-empty list")
    if len(set(result)) != len(result):
        raise ValueError(f"{field_name} must not contain duplicates")
    return result


def _positive_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a positive integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a positive integer") from exc
    if result <= 0 or result != value:
        raise ValueError(f"{field_name} must be a positive integer")
    return result


def _positive_number(value: Any, field_name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be positive")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be positive") from exc
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{field_name} must be positive")
    return result


def _finite_non_negative_number(value: Any, field_name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a finite non-negative number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a finite non-negative number") from exc
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{field_name} must be a finite non-negative number")
    return result


def _integer(value: Any, field_name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be an integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an integer") from exc
    if result != value:
        raise ValueError(f"{field_name} must be an integer")
    return result


def _strict_bool(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field_name} must be a boolean")
    return value


def _suite_options(manifest: Any, value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("options must be an object")
    if not all(isinstance(key, str) and key for key in value):
        raise ValueError("options keys must be non-empty strings")
    declarations = {item.name: item for item in manifest.options}
    unknown = sorted(set(value) - set(declarations))
    if unknown:
        raise ValueError(f"unknown Suite options: {', '.join(unknown)}")
    resolved: dict[str, Any] = {}
    for name, option in declarations.items():
        raw = value.get(name, option.default)
        if raw is None:
            if option.required:
                raise ValueError(f"Suite option {name} is required")
            continue
        if option.type == "boolean":
            raw = _strict_bool(raw, f"options.{name}")
        elif option.type == "integer":
            raw = _integer(raw, f"options.{name}")
            if option.minimum is not None and raw < option.minimum:
                raise ValueError(f"options.{name} is below its minimum")
            if option.maximum is not None and raw > option.maximum:
                raise ValueError(f"options.{name} exceeds its maximum")
        elif option.type in {"string", "enum"}:
            if not isinstance(raw, str) or not raw.strip() or len(raw) > 512:
                raise ValueError(f"options.{name} must be non-empty text")
            raw = raw.strip()
            if option.type == "enum" and raw not in option.choices:
                raise ValueError(f"options.{name} must be one of: {', '.join(option.choices)}")
            if re.search(r"https?://", raw, re.IGNORECASE):
                raise ValueError(f"options.{name} cannot contain an HTTP endpoint")
            if Path(raw).is_absolute() or re.match(r"^[A-Za-z]:[\\/]", raw):
                raise ValueError(f"options.{name} cannot contain an absolute path")
        else:  # pragma: no cover - manifest parser owns this invariant
            raise ValueError(f"unsupported Suite option type: {option.type}")
        resolved[name] = raw
    try:
        encoded = json.dumps(resolved, allow_nan=False, sort_keys=True).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("options must be finite canonical JSON") from exc
    if len(encoded) > 8192:
        raise ValueError("options exceed the 8192-byte limit")
    return resolved


def _check(
    code: str,
    label: str,
    ok: bool,
    detail: str,
    action: str = "",
) -> dict[str, Any]:
    return {
        "code": code,
        "label": label,
        "ok": bool(ok),
        "detail": detail,
        "action": action,
    }


def _record_event(
    output: Path,
    callback: ProgressCallback | None,
    **payload: Any,
) -> None:
    sanitized = redact_payload(
        payload,
        secrets=collect_env_secrets(),
        roots={"evaluation": output, "repository": Path.cwd()},
    )
    if (output / "progress.jsonl").is_symlink():
        raise ValueError("Evaluation progress.jsonl must not be a symlink")
    path = contained_artifact_path(output, "progress.jsonl")
    _append_jsonl(path, sanitized)
    if callback is not None:
        callback(sanitized)


def _hash_json(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _write_json(path: Path, payload: Any) -> None:
    _write_text(
        path,
        json.dumps(payload, allow_nan=False, ensure_ascii=False, indent=2) + "\n",
    )


def _append_jsonl(path: Path, payload: Any) -> None:
    _ensure_private_dir(path.parent)
    if path.is_symlink():
        raise ValueError(f"Evaluation artifact cannot be a symlink: {path.name}")
    flags = os.O_WRONLY | os.O_APPEND | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    created = False
    try:
        descriptor = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
    except FileExistsError:
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise ValueError(f"Evaluation artifact cannot be opened safely: {path.name}") from exc
    except OSError as exc:
        raise ValueError(f"Evaluation artifact cannot be created safely: {path.name}") from exc
    try:
        if created and os.name != "nt":
            os.fchmod(descriptor, 0o600)
        _validate_private_artifact_metadata(
            os.fstat(descriptor),
            path,
            label="Evaluation artifact",
        )
        with os.fdopen(descriptor, "a", encoding="utf-8", newline="\n") as handle:
            descriptor = -1
            handle.write(
                json.dumps(
                    payload,
                    allow_nan=False,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n"
            )
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _write_text(path: Path, content: str) -> None:
    _ensure_private_dir(path.parent)
    if path.is_symlink():
        raise ValueError(f"Evaluation artifact cannot be a symlink: {path.name}")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _ensure_private_dir(path: Path) -> None:
    if path.is_symlink():
        raise ValueError(f"Evaluation directory cannot be a symlink: {path.name}")
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    if not path.is_dir():
        raise ValueError(f"Evaluation path is not a directory: {path.name}")
    if os.name != "nt":
        path.chmod(0o700)


def _validate_private_artifact_metadata(
    metadata: os.stat_result,
    path: Path,
    *,
    label: str,
) -> None:
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"{label} must be a regular file: {path.name}")
    if os.name == "nt":
        return
    if metadata.st_uid != os.getuid():
        raise PermissionError(f"{label} must be owned by the current user: {path.name}")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise PermissionError(f"{label} must use mode 0600: {path.name}")
    if metadata.st_nlink != 1:
        raise ValueError(f"{label} must have exactly one hard link: {path.name}")


def _read_private_json_object(path: Path, label: str) -> dict[str, Any]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError(f"{label} cannot be opened safely") from exc
    try:
        _validate_private_artifact_metadata(
            os.fstat(descriptor),
            path,
            label=label,
        )
        try:
            with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
                descriptor = -1
                payload = json.load(handle)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"{label} is unreadable") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be an object")
    return payload


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is unreadable") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be an object")
    return payload


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_evaluation_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"eval-{timestamp}-{uuid.uuid4().hex[:8]}"


def _bot_id(bot: str) -> str:
    value = str(bot).strip()
    if any(char in value for char in ("/", "\\")):
        path = Path(value)
        return path.parent.name if path.name == "bot.yaml" else path.name
    return value


def _portable_bot_ref(bot: str) -> str:
    value = str(bot).strip()
    if not value or not any(char in value for char in ("/", "\\")):
        return value
    path = Path(value)
    try:
        return path.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return value


__all__ = [
    "CaseComparison",
    "ComparisonEvaluationRequest",
    "ComparisonPreset",
    "EvaluationKind",
    "EvaluationRequest",
    "EvaluationResult",
    "EvaluationStatus",
    "EvaluationTarget",
    "EvaluationTrial",
    "EvaluationValidationError",
    "SuiteEvaluationRequest",
    "TargetExecutor",
    "TrialExecutionRequest",
    "TrialOutcome",
    "aggregate_comparison",
    "evaluation_result_to_dict",
    "execute_evaluation_trial",
    "parse_evaluation_request",
    "run_evaluation",
    "validate_evaluation",
]
