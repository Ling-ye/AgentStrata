"""Persistent control-plane manager for unified evaluations."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable, Iterator, Literal, Mapping, Sequence

from chatcopilot.evals.redaction import collect_env_secrets, sanitize_text
from console.control.discovery import repo_root
from console.control.evals import _bot_env, _bot_spec_path, _temporary_env
from console.control.instances import BotInstance
from console.control.operations import KEEPALIVE

ACTIVE_STATUSES = {"queued", "running"}
TERMINAL_STATUSES = {
    "completed",
    "partial",
    "cancelled",
    "interrupted",
    "error",
}
ValidationResult = Mapping[str, Any]
Validator = Callable[[BotInstance, Mapping[str, Any]], ValidationResult]
WorkerPidStatus = Literal["matched", "unknown", "exited"]
_ROOT_LOCKS_GUARD = threading.Lock()
_ROOT_LOCKS: dict[str, threading.RLock] = {}


def _root_thread_lock(root: Path) -> threading.RLock:
    key = str(root)
    with _ROOT_LOCKS_GUARD:
        return _ROOT_LOCKS.setdefault(key, threading.RLock())


@contextmanager
def _locked_file(path: Path) -> Iterator[None]:
    """Hold a process-safe advisory lock for a short control-plane mutation."""

    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    locked = False
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            getattr(msvcrt, "locking")(
                handle.fileno(),
                getattr(msvcrt, "LK_LOCK"),
                1,
            )
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        locked = True
        yield
    finally:
        try:
            if locked:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    getattr(msvcrt, "locking")(
                        handle.fileno(),
                        getattr(msvcrt, "LK_UNLCK"),
                        1,
                    )
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


class EvaluationBlocked(ValueError):
    """Raised when an evaluation request fails readiness checks."""

    def __init__(self, payload: Mapping[str, Any]) -> None:
        self.payload = dict(payload)
        super().__init__(str(self.payload.get("message") or "evaluation validation failed"))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(dict(payload), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _default_validator(
    instance: BotInstance,
    request: Mapping[str, Any],
) -> ValidationResult:
    from chatcopilot.evals.evaluations import validate_evaluation

    with _temporary_env(_bot_env(instance)):
        return validate_evaluation(_core_request(instance, request))


def _core_request(
    instance: BotInstance,
    request: Mapping[str, Any],
    *,
    evaluation_id: str | None = None,
) -> dict[str, Any]:
    kind = str(request.get("kind") or "")
    common = {
        "kind": kind,
        "bot": str(_bot_spec_path(instance)),
    }
    if evaluation_id:
        common["evaluation_id"] = evaluation_id
    if kind == "comparison":
        value = {
            **common,
            "profile": str(
                request.get("profile_id") or request.get("profile") or ""
            ),
            "preset": str(request.get("preset") or "quick"),
        }
        if value["preset"] == "custom":
            value.update(
                {
                    "targets": list(
                        request.get("target_ids")
                        or request.get("targets")
                        or ()
                    ),
                    "case_refs": list(request.get("case_refs") or ()),
                    "repetitions": request.get("repetitions"),
                    "max_wall_seconds": request.get("max_wall_seconds"),
                    "seed": request.get("seed"),
                }
            )
        return value
    if kind == "suite":
        return {
            **common,
            "suite": str(
                request.get("suite_id") or request.get("suite") or ""
            ),
            "case_ids": list(request.get("case_ids") or ()),
            "dry_run": bool(request.get("dry_run", False)),
            "llm_judge": bool(request.get("llm_judge", False)),
        }
    raise ValueError(f"unsupported evaluation kind: {kind}")


def _stored_request(
    instance: BotInstance,
    request: Mapping[str, Any],
    effective: Mapping[str, Any],
    targets: Sequence[Any],
) -> dict[str, Any]:
    kind = str(request.get("kind") or "")
    value: dict[str, Any] = {
        "kind": kind,
        "bot_id": instance.instance_id,
    }
    if kind == "comparison":
        value.update(
            {
                "profile_id": str(
                    effective.get("profile")
                    or request.get("profile_id")
                    or ""
                ),
                "preset": str(
                    effective.get("preset")
                    or request.get("preset")
                    or "quick"
                ),
                "target_ids": list(
                    effective.get("targets")
                    or request.get("target_ids")
                    or ()
                ),
                "case_refs": list(
                    effective.get("case_refs")
                    or request.get("case_refs")
                    or ()
                ),
                "repetitions": effective.get(
                    "repetitions",
                    request.get("repetitions"),
                ),
                "max_wall_seconds": effective.get(
                    "max_wall_seconds",
                    request.get("max_wall_seconds"),
                ),
                "seed": effective.get("seed", request.get("seed")),
            }
        )
    elif kind == "suite":
        value.update(
            {
                "suite_id": str(
                    effective.get("suite")
                    or request.get("suite_id")
                    or ""
                ),
                "case_ids": list(
                    effective.get("case_ids")
                    or request.get("case_ids")
                    or ()
                ),
                "dry_run": bool(
                    effective.get(
                        "dry_run",
                        request.get("dry_run", False),
                    )
                ),
                "llm_judge": bool(
                    effective.get(
                        "llm_judge",
                        request.get("llm_judge", False),
                    )
                ),
            }
        )
    else:
        raise ValueError(f"unsupported evaluation kind: {kind}")
    value["targets"] = [
        dict(item) if isinstance(item, Mapping) else item for item in targets
    ]
    return value


class EvaluationManager:
    def __init__(
        self,
        root: Path | None = None,
        *,
        validator: Validator | None = None,
    ) -> None:
        self.root = (
            root or repo_root() / "reports" / "evals" / "evaluations"
        ).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._validator = validator or _default_validator
        self._lock = threading.RLock()
        self._processes: dict[str, subprocess.Popen[str]] = {}
        self._process_bot_ids: dict[str, str] = {}
        self._cancelled: set[str] = set()
        self._recover_interrupted()

    def start(
        self,
        *,
        instance: BotInstance,
        request: Mapping[str, Any],
    ) -> dict[str, Any]:
        clean_request = dict(request)
        clean_request["bot_id"] = instance.instance_id
        with self._creation_guard(), self._lock:
            active = self._active_for_bot_locked(instance.instance_id)
            if active is not None:
                raise RuntimeError(
                    f"Bot {instance.instance_id} already has an active evaluation: "
                    f"{active['evaluation_id']}"
                )

        validation = dict(self._validator(instance, clean_request))
        if not validation.get("ready"):
            payload = {
                "code": "evaluation_blocked",
                "message": str(
                    validation.get("message")
                    or "评测条件未满足，未创建评测记录"
                ),
                "checks": list(validation.get("checks") or ()),
            }
            raise EvaluationBlocked(payload)

        effective = validation.get("effective_request")
        if not isinstance(effective, Mapping):
            effective = {}
        targets = validation.get("targets")
        target_values: Sequence[Any] = ()
        if isinstance(targets, Sequence) and not isinstance(
            targets,
            (str, bytes),
        ):
            target_values = targets
        clean_request = _stored_request(
            instance,
            clean_request,
            effective,
            target_values,
        )

        with self._creation_guard(), self._lock:
            active = self._active_for_bot_locked(instance.instance_id)
            if active is not None:
                raise RuntimeError(
                    f"Bot {instance.instance_id} already has an active evaluation: "
                    f"{active['evaluation_id']}"
                )
            evaluation_id = (
                "eval-"
                + datetime.now().strftime("%Y%m%d-%H%M%S")
                + "-"
                + uuid.uuid4().hex[:8]
            )
            directory = self._evaluation_dir(evaluation_id)
            created_at = _utc_now()
            stored_request = {
                **clean_request,
                "evaluation_id": evaluation_id,
                "bot_id": instance.instance_id,
                "bot_spec": str(_bot_spec_path(instance).relative_to(repo_root())),
                "created_at": created_at,
            }
            state = {
                "evaluation_id": evaluation_id,
                "kind": str(clean_request.get("kind") or ""),
                "status": "queued",
                "pid": None,
                "started_at": None,
                "finished_at": None,
                "duration_seconds": None,
                "completed_trials": 0,
                "planned_trials": self._planned_trial_count(
                    stored_request,
                    {},
                ),
                "error": None,
            }
            try:
                self._create_claim(instance.instance_id, evaluation_id)
                directory.mkdir(parents=True)
                _write_json(directory / "request.json", stored_request)
                _write_json(directory / "state.json", state)
                self._spawn(evaluation_id, instance, stored_request)
            except Exception as exc:
                safe_error = self._sanitize_startup_error(
                    instance,
                    directory,
                    exc,
                )
                state.update(
                    {
                        "status": "error",
                        "finished_at": _utc_now(),
                        "error": safe_error,
                    }
                )
                if directory.is_dir():
                    _write_json(directory / "state.json", state)
                process = self._processes.pop(evaluation_id, None)
                self._process_bot_ids.pop(evaluation_id, None)
                if process is not None:
                    self._terminate_process(process)
                self._release_claim(instance.instance_id, evaluation_id)
                raise RuntimeError(safe_error) from exc
        return self.get(evaluation_id)

    def list(
        self,
        *,
        kind: str | None = None,
        bot_id: str | None = None,
        target: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        values: list[dict[str, Any]] = []
        for path in self.root.iterdir():
            if not path.is_dir():
                continue
            try:
                item = self.get(path.name, include_result=False)
            except (KeyError, ValueError):
                continue
            if kind and item.get("kind") != kind:
                continue
            if bot_id and item.get("bot_id") != bot_id:
                continue
            if status and item.get("status") != status:
                continue
            if target and target not in {
                str(value.get("target_id") or "")
                for value in item.get("targets", [])
                if isinstance(value, Mapping)
            }:
                continue
            values.append(item)
        values.sort(
            key=lambda value: str(value.get("created_at") or ""),
            reverse=True,
        )
        return values

    def get(
        self,
        evaluation_id: str,
        *,
        include_result: bool = True,
    ) -> dict[str, Any]:
        directory = self._verified_evaluation_dir(evaluation_id)
        state = _read_json(directory / "state.json")
        request = _read_json(directory / "request.json")
        result = self._verified_result(
            evaluation_id,
            directory=directory,
            required=False,
        )
        trials = result.get("trials")
        trial_values = trials if isinstance(trials, list) else []
        targets = result.get("targets")
        if not isinstance(targets, list):
            targets = request.get("targets")
        target_values = targets if isinstance(targets, list) else []
        total = self._planned_trial_count(request, result)
        completed = len(trial_values)
        progress = {
            "completed": completed,
            "total": total,
            "percent": min(100, round(completed / total * 100)) if total else 0,
        }
        response = {
            **state,
            "kind": str(request.get("kind") or state.get("kind") or ""),
            "bot_id": str(request.get("bot_id") or ""),
            "created_at": str(request.get("created_at") or ""),
            "targets": target_values,
            "progress": progress,
            "duration_seconds": result.get(
                "duration_seconds",
                state.get("duration_seconds"),
            ),
            "summary": result.get("summary")
            if isinstance(result.get("summary"), Mapping)
            else {},
            "selection": self._selection_summary(request),
        }
        if include_result:
            response["request"] = request
            response["result"] = result
        return response

    def case_detail(
        self,
        evaluation_id: str,
        case_ref: str,
    ) -> dict[str, Any]:
        directory = self._verified_evaluation_dir(evaluation_id)
        result = self._verified_result(
            evaluation_id,
            directory=directory,
            required=True,
        )
        comparisons = result.get("comparisons")
        comparison = next(
            (
                value
                for value in comparisons
                if isinstance(value, Mapping)
                and str(value.get("case_ref") or value.get("case_id") or "")
                == case_ref
            ),
            None,
        ) if isinstance(comparisons, list) else None
        trials = [
            value
            for value in result.get("trials", [])
            if isinstance(value, Mapping)
            and case_ref
            in {
                str(value.get("case_ref") or ""),
                str(value.get("case_id") or ""),
            }
        ]
        if not trials and comparison is None:
            raise KeyError(case_ref)
        return {
            "case_ref": case_ref,
            "comparison": comparison,
            "trials": trials,
        }

    def active_for_bot(self, bot_id: str) -> dict[str, Any] | None:
        with self._creation_guard(), self._lock:
            return self._active_for_bot_locked(bot_id)

    def clone(
        self,
        evaluation_id: str,
        *,
        instance: BotInstance,
    ) -> dict[str, Any]:
        directory = self._verified_evaluation_dir(evaluation_id)
        stored = _read_json(directory / "request.json")
        return self.start(
            instance=instance,
            request=self._clone_request(stored),
        )

    def cancel(self, evaluation_id: str) -> dict[str, Any]:
        bot_id = ""
        external_pid: int | None = None
        worker_identity_unknown = False
        with self._creation_guard(), self._lock:
            state = self._state(evaluation_id)
            if state.get("status") not in ACTIVE_STATUSES:
                raise RuntimeError(
                    "only queued or running evaluations can be cancelled"
                )
            request = _read_json(
                self._evaluation_dir(evaluation_id) / "request.json"
            )
            bot_id = str(request.get("bot_id") or "")
            process = self._processes.get(evaluation_id)
            if process is None or process.poll() is not None:
                (
                    external_pid,
                    worker_identity_unknown,
                ) = self._managed_worker_observation(
                    evaluation_id,
                    bot_id=bot_id,
                    state=state,
                )
            if worker_identity_unknown:
                raise RuntimeError(
                    "evaluation worker still exists but its identity "
                    "cannot be verified; cancellation refused"
                )
            self._cancelled.add(evaluation_id)
        if process is not None and process.poll() is None:
            self._terminate_process(process)
        elif external_pid is not None:
            self._terminate_pid(external_pid)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with self._creation_guard(), self._lock:
                current_state = self._state(evaluation_id)
                if not self._evaluation_is_live(
                    evaluation_id,
                    bot_id=bot_id,
                    state=current_state,
                ):
                    self._finalize_cancelled(evaluation_id)
                    if bot_id:
                        self._release_claim(bot_id, evaluation_id)
                    return self.get(evaluation_id, include_result=False)
            time.sleep(0.05)
        raise RuntimeError("evaluation process did not stop after cancellation")

    def delete(self, evaluation_id: str) -> None:
        with self._creation_guard(), self._lock:
            state = self._state(evaluation_id)
            request = _read_json(
                self._verified_evaluation_dir(evaluation_id) / "request.json"
            )
            bot_id = str(request.get("bot_id") or "")
            if self._evaluation_is_live(
                evaluation_id,
                bot_id=bot_id,
                state=state,
            ):
                raise RuntimeError("running evaluations cannot be deleted")
            if state.get("status") in ACTIVE_STATUSES:
                self._reconcile_stopped_evaluation(
                    evaluation_id,
                    "evaluation process is no longer running",
                )
            target = self._verified_evaluation_dir(evaluation_id)
            target.relative_to(self.root)
            if target == self.root or not target.is_dir():
                raise ValueError("invalid evaluation directory")
            shutil.rmtree(target)
            if bot_id:
                self._release_claim(bot_id, evaluation_id)

    def coverage(
        self,
        *,
        bot_id: str | None = None,
    ) -> dict[str, Any]:
        buckets: dict[tuple[str, str, str], dict[str, Any]] = {}
        for evaluation in self.list(bot_id=bot_id):
            evaluation_id = str(evaluation["evaluation_id"])
            result = self._verified_result(
                evaluation_id,
                required=False,
            )
            for trial in result.get("trials", []):
                if not isinstance(trial, Mapping):
                    continue
                case_ref = str(
                    trial.get("case_ref")
                    or ":".join(
                        part
                        for part in (
                            str(trial.get("suite_id") or ""),
                            str(trial.get("case_id") or ""),
                        )
                        if part
                    )
                )
                fingerprint = str(
                    trial.get("target_fingerprint")
                    or trial.get("fingerprint")
                    or ""
                )
                current_bot = str(evaluation.get("bot_id") or "")
                if not current_bot or not case_ref or not fingerprint:
                    continue
                key = (current_bot, case_ref, fingerprint)
                target_id = str(trial.get("target_id") or "")
                history_item = {
                    "trial_id": str(trial.get("trial_id") or ""),
                    "attempt": trial.get("attempt"),
                    "evaluation_id": evaluation.get("evaluation_id"),
                    "kind": evaluation.get("kind"),
                    "bot_id": current_bot,
                    "suite_id": str(trial.get("suite_id") or ""),
                    "case_id": str(trial.get("case_id") or ""),
                    "case_ref": case_ref,
                    "category": str(trial.get("dimension") or ""),
                    "target_id": target_id,
                    "target_fingerprint": fingerprint,
                    "executor": str(trial.get("executor") or ""),
                    "backend": str(trial.get("backend") or ""),
                    "model": str(trial.get("model") or ""),
                    "reasoning_effort": str(
                        trial.get("reasoning_effort") or ""
                    ),
                    "outcome": str(trial.get("outcome") or ""),
                    "score": trial.get("score"),
                    "max_score": trial.get("max_score"),
                    "duration_seconds": trial.get("duration_seconds"),
                    "finished_at": str(
                        trial.get("finished_at")
                        or evaluation.get("finished_at")
                        or ""
                    ),
                    "error": str(trial.get("error") or ""),
                }
                bucket = buckets.setdefault(
                    key,
                    {
                        "bot_id": current_bot,
                        "suite_id": str(trial.get("suite_id") or ""),
                        "case_id": str(trial.get("case_id") or ""),
                        "case_ref": case_ref,
                        "category": str(trial.get("dimension") or ""),
                        "summary": "",
                        "target_id": target_id,
                        "target_fingerprint": fingerprint,
                        "completed_count": 0,
                        "history": [],
                    },
                )
                bucket["completed_count"] += 1
                bucket["history"].append(history_item)

        records: list[dict[str, Any]] = []
        for bucket in buckets.values():
            history = sorted(
                bucket["history"],
                key=lambda value: (
                    str(value.get("finished_at") or ""),
                    str(value.get("evaluation_id") or ""),
                ),
                reverse=True,
            )
            latest = history[0]
            records.append(
                {
                    **{key: value for key, value in bucket.items() if key != "history"},
                    "last_evaluation_id": latest["evaluation_id"],
                    "last_outcome": latest["outcome"],
                    "last_score": latest["score"],
                    "last_max_score": latest["max_score"],
                    "last_duration_seconds": latest["duration_seconds"],
                    "last_completed_at": latest["finished_at"],
                    "history": history,
                }
            )
        records.sort(
            key=lambda value: (
                str(value.get("last_completed_at") or ""),
                str(value.get("case_ref") or ""),
            ),
            reverse=True,
        )
        return {
            "generated_at": _utc_now(),
            "summary": {
                "case_count": len(
                    {
                        (item["bot_id"], item["case_ref"])
                        for item in records
                    }
                ),
                "failed_case_count": len(
                    {
                        (item["bot_id"], item["case_ref"])
                        for item in records
                        if item.get("last_outcome") in {"failed", "error"}
                    }
                ),
                "bot_count": len({item["bot_id"] for item in records}),
                "target_count": len(
                    {item["target_fingerprint"] for item in records}
                ),
            },
            "records": records,
        }

    def report_path(self, evaluation_id: str, kind: str) -> Path:
        names = {"json": "result.json", "markdown": "summary.md"}
        if kind not in names:
            raise ValueError("report kind must be json or markdown")
        directory = self._verified_evaluation_dir(evaluation_id)
        self._verified_result(
            evaluation_id,
            directory=directory,
            required=True,
        )
        path = directory / names[kind]
        self._verify_artifact_file(path, required=True)
        if not path.is_file():
            raise KeyError(evaluation_id)
        return path

    def follow(
        self,
        evaluation_id: str,
        *,
        poll_seconds: float = 0.25,
    ) -> Iterator[dict[str, Any] | str]:
        directory = self._verified_evaluation_dir(evaluation_id)
        self._verified_result(
            evaluation_id,
            directory=directory,
            required=False,
        )
        path = directory / "progress.jsonl"
        position = 0
        idle_since = time.monotonic()
        while True:
            self._verified_evaluation_dir(evaluation_id)
            self._verified_result(
                evaluation_id,
                directory=directory,
                required=False,
            )
            self._verify_artifact_file(path, required=False)
            if path.is_file():
                with path.open(
                    "r",
                    encoding="utf-8",
                    errors="replace",
                ) as handle:
                    handle.seek(position)
                    for line in handle:
                        try:
                            payload = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(payload, dict):
                            yield payload
                    position = handle.tell()
            state = _read_json(directory / "state.json")
            if state.get("status") not in ACTIVE_STATUSES:
                yield {
                    "event": "evaluation_status",
                    "evaluation_id": evaluation_id,
                    "status": state.get("status", "error"),
                }
                return
            if time.monotonic() - idle_since >= 15:
                idle_since = time.monotonic()
                yield KEEPALIVE
            time.sleep(poll_seconds)

    def close(self) -> None:
        with self._creation_guard(), self._lock:
            processes = [
                (
                    evaluation_id,
                    process,
                    self._process_bot_ids.get(evaluation_id, ""),
                )
                for evaluation_id, process in self._processes.items()
            ]
            for evaluation_id, _process, _bot_id in processes:
                try:
                    self._reconcile_stopped_evaluation(
                        evaluation_id,
                        "console backend stopped before completion",
                    )
                except OSError:
                    continue
        for _evaluation_id, process, _bot_id in processes:
            self._terminate_process(process)
        with self._creation_guard(), self._lock:
            for evaluation_id, process, bot_id in processes:
                if process.poll() is None:
                    continue
                self._processes.pop(evaluation_id, None)
                self._process_bot_ids.pop(evaluation_id, None)
                if bot_id:
                    self._release_claim(bot_id, evaluation_id)

    def _spawn(
        self,
        evaluation_id: str,
        instance: BotInstance,
        request: Mapping[str, Any],
    ) -> None:
        directory = self._evaluation_dir(evaluation_id)
        command = [
            sys.executable,
            "-m",
            "chatcopilot",
            "evals",
            "run",
            "--request",
            json.dumps(
                _core_request(
                    instance,
                    request,
                    evaluation_id=evaluation_id,
                ),
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            "--output",
            str(directory),
        ]

        env = os.environ.copy()
        env.update(_bot_env(instance))
        src = str(repo_root() / "src")
        env["PYTHONPATH"] = os.pathsep.join(
            [
                src,
                *[
                    item
                    for item in env.get("PYTHONPATH", "").split(os.pathsep)
                    if item
                ],
            ]
        )
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        process = subprocess.Popen(
            command,
            cwd=str(repo_root()),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            start_new_session=os.name != "nt",
            creationflags=flags,
        )
        self._processes[evaluation_id] = process
        self._process_bot_ids[evaluation_id] = instance.instance_id
        state = self._state(evaluation_id)
        state.update(
            {
                "status": "running",
                "started_at": _utc_now(),
                "pid": process.pid,
            }
        )
        _write_json(directory / "state.json", state)
        self._update_claim(
            instance.instance_id,
            evaluation_id,
            worker_pid=process.pid,
        )
        secrets = collect_env_secrets(env)
        threading.Thread(
            target=self._monitor,
            args=(
                evaluation_id,
                process,
                secrets,
                instance.instance_id,
            ),
            name=f"evaluation-{evaluation_id}",
            daemon=True,
        ).start()

    def _monitor(
        self,
        evaluation_id: str,
        process: subprocess.Popen[str],
        secrets: Sequence[str],
        bot_id: str,
    ) -> None:
        directory = self._evaluation_dir(evaluation_id)
        assert process.stdout is not None
        try:
            with (directory / "run.log").open(
                "a",
                encoding="utf-8",
                buffering=1,
            ) as log_handle:
                for raw_line in process.stdout:
                    clean = sanitize_text(
                        raw_line.rstrip("\r\n"),
                        secrets=secrets,
                        roots={
                            "evaluation": directory,
                            "repository": repo_root(),
                        },
                    )
                    log_handle.write(clean + "\n")
        except OSError:
            for _raw_line in process.stdout:
                pass
        exit_code = process.wait()
        with self._creation_guard(), self._lock:
            try:
                state_path = directory / "state.json"
                self._verify_artifact_file(state_path, required=False)
                state = _read_json(state_path)
                if not state or not directory.is_dir():
                    return
                result = self._verified_result(
                    evaluation_id,
                    directory=directory,
                    required=False,
                )
                if evaluation_id in self._cancelled:
                    status = "cancelled"
                    self._patch_report_status(
                        evaluation_id,
                        status,
                        "evaluation cancelled by user",
                    )
                elif result.get("status") in TERMINAL_STATUSES:
                    status = str(result["status"])
                else:
                    status = "error"
                    state["error"] = (
                        "evaluation process exited without a final result"
                    )
                state.update(
                    {
                        "status": status,
                        "finished_at": _utc_now(),
                        "pid": None,
                    }
                )
                if exit_code and not state.get("error"):
                    state["error"] = (
                        f"evaluation process exited with code {exit_code}"
                    )
                _write_json(directory / "state.json", state)
            except (OSError, ValueError):
                pass
            finally:
                self._processes.pop(evaluation_id, None)
                self._process_bot_ids.pop(evaluation_id, None)
                self._cancelled.discard(evaluation_id)
                self._release_claim(bot_id, evaluation_id)

    def _recover_interrupted(self) -> None:
        with self._creation_guard(), self._lock:
            for path in self.root.iterdir():
                if not path.is_dir():
                    continue
                try:
                    directory = self._evaluation_dir(path.name)
                except ValueError:
                    continue
                state_path = directory / "state.json"
                request_path = directory / "request.json"
                self._verify_artifact_file(state_path, required=False)
                self._verify_artifact_file(request_path, required=False)
                state = _read_json(state_path)
                request = _read_json(request_path)
                bot_id = str(request.get("bot_id") or "")
                if self._evaluation_is_live(
                    path.name,
                    bot_id=bot_id,
                    state=state,
                ):
                    continue
                if state.get("status") in ACTIVE_STATUSES:
                    self._reconcile_stopped_evaluation(
                        path.name,
                        "console backend restarted before completion",
                    )
                if bot_id:
                    self._release_claim(bot_id, path.name)
            self._remove_stale_orphan_claims()

    def _reconcile_stopped_evaluation(
        self,
        evaluation_id: str,
        message: str,
    ) -> None:
        directory = self._evaluation_dir(evaluation_id)
        if not directory.is_dir():
            return
        result = self._verified_result(
            evaluation_id,
            directory=directory,
            required=False,
        )
        result_status = str(result.get("status") or "")
        if result_status not in TERMINAL_STATUSES:
            self._mark_interrupted(evaluation_id, message)
            return
        state = _read_json(directory / "state.json")
        if not state:
            return
        trials = result.get("trials")
        completed_trials = len(trials) if isinstance(trials, list) else 0
        state.update(
            {
                "status": result_status,
                "pid": None,
                "started_at": result.get(
                    "started_at",
                    state.get("started_at"),
                ),
                "finished_at": result.get(
                    "finished_at",
                    state.get("finished_at"),
                ),
                "duration_seconds": result.get(
                    "duration_seconds",
                    state.get("duration_seconds"),
                ),
                "completed_trials": completed_trials,
                "planned_trials": result.get(
                    "planned_trials",
                    state.get("planned_trials"),
                ),
                "error": result.get("error"),
            }
        )
        _write_json(directory / "state.json", state)

    def _mark_interrupted(
        self,
        evaluation_id: str,
        message: str,
    ) -> None:
        directory = self._evaluation_dir(evaluation_id)
        if not directory.is_dir():
            return
        result = self._verified_result(
            evaluation_id,
            directory=directory,
            required=False,
        )
        trials = result.get("trials")
        completed = len(trials) if isinstance(trials, list) else 0
        status = "partial" if completed else "interrupted"
        state = _read_json(directory / "state.json")
        if not state:
            return
        state.update(
            {
                "status": status,
                "finished_at": _utc_now(),
                "pid": None,
                "error": message,
            }
        )
        _write_json(directory / "state.json", state)
        self._patch_report_status(evaluation_id, status, message)

    def _finalize_cancelled(self, evaluation_id: str) -> None:
        state = self._state(evaluation_id)
        state.update(
            {
                "status": "cancelled",
                "finished_at": _utc_now(),
                "pid": None,
                "error": "evaluation cancelled by user",
            }
        )
        _write_json(
            self._evaluation_dir(evaluation_id) / "state.json",
            state,
        )
        self._patch_report_status(
            evaluation_id,
            "cancelled",
            "evaluation cancelled by user",
        )
        self._cancelled.discard(evaluation_id)

    @staticmethod
    def _sanitize_startup_error(
        instance: BotInstance,
        directory: Path,
        exc: Exception,
    ) -> str:
        env = os.environ.copy()
        try:
            env.update(_bot_env(instance))
        except Exception:
            pass
        return sanitize_text(
            f"{type(exc).__name__}: {exc}",
            secrets=collect_env_secrets(env),
            roots={
                "evaluation": directory,
                "repository": repo_root(),
            },
        )

    @contextmanager
    def _creation_guard(self) -> Iterator[None]:
        thread_lock = _root_thread_lock(self.root)
        with thread_lock, _locked_file(self.root / ".manager.lock"):
            yield

    def _active_for_bot_locked(
        self,
        bot_id: str,
    ) -> dict[str, Any] | None:
        claim = self._read_claim(bot_id)
        if claim is not None:
            evaluation_id = str(claim.get("evaluation_id") or "")
            if not evaluation_id:
                raise RuntimeError(
                    f"Bot {bot_id} has an invalid evaluation activity claim"
                )
            directory = self._evaluation_dir(evaluation_id)
            state_path = directory / "state.json"
            self._verify_artifact_file(state_path, required=False)
            state = _read_json(state_path)
            if self._claim_is_live(claim, directory):
                try:
                    return self.get(evaluation_id, include_result=False)
                except KeyError:
                    return {
                        "evaluation_id": evaluation_id,
                        "bot_id": bot_id,
                        "status": "running",
                    }
            if state.get("status") in ACTIVE_STATUSES:
                self._reconcile_stopped_evaluation(
                    evaluation_id,
                    "evaluation process is no longer running",
                )
            self._release_claim(bot_id, evaluation_id)

        for item in self.list(bot_id=bot_id):
            evaluation_id = str(item.get("evaluation_id") or "")
            state = _read_json(
                self._evaluation_dir(evaluation_id) / "state.json"
            )
            if self._evaluation_is_live(
                evaluation_id,
                bot_id=bot_id,
                state=state,
            ):
                return item
            if state.get("status") in ACTIVE_STATUSES:
                self._reconcile_stopped_evaluation(
                    evaluation_id,
                    "evaluation process is no longer running",
                )
                self._release_claim(bot_id, evaluation_id)
        return None

    def _evaluation_is_live(
        self,
        evaluation_id: str,
        *,
        bot_id: str,
        state: Mapping[str, Any],
    ) -> bool:
        process = self._processes.get(evaluation_id)
        if process is not None and process.poll() is None:
            return True
        directory = self._evaluation_dir(evaluation_id)
        pid = state.get("pid")
        if (
            isinstance(pid, int)
            and self._worker_pid_status(pid, directory) != "exited"
        ):
            return True
        if bot_id:
            claim = self._read_claim(bot_id)
            if (
                claim is not None
                and claim.get("evaluation_id") == evaluation_id
                and self._claim_is_live(claim, directory)
            ):
                return True
        return False

    def _managed_worker_observation(
        self,
        evaluation_id: str,
        *,
        bot_id: str,
        state: Mapping[str, Any],
    ) -> tuple[int | None, bool]:
        candidates: list[int] = []
        state_pid = state.get("pid")
        if isinstance(state_pid, int):
            candidates.append(state_pid)
        if bot_id:
            claim = self._read_claim(bot_id)
            if (
                claim is not None
                and claim.get("evaluation_id") == evaluation_id
            ):
                worker_pid = claim.get("worker_pid")
                if (
                    isinstance(worker_pid, int)
                    and worker_pid not in candidates
                ):
                    candidates.append(worker_pid)
        directory = self._evaluation_dir(evaluation_id)
        identity_unknown = False
        for pid in candidates:
            status = self._worker_pid_status(pid, directory)
            if status == "matched":
                return pid, False
            if status == "unknown":
                identity_unknown = True
        return None, identity_unknown

    def _claim_path(self, bot_id: str) -> Path:
        digest = sha256(bot_id.encode("utf-8")).hexdigest()[:24]
        return self.root / f".active-{digest}.json"

    def _read_claim(self, bot_id: str) -> dict[str, Any] | None:
        path = self._claim_path(bot_id)
        if path.is_symlink():
            raise RuntimeError(
                f"Bot {bot_id} evaluation activity claim cannot be a symlink"
            )
        if not path.exists():
            return None
        claim = _read_json(path)
        if not claim or claim.get("bot_id") != bot_id:
            raise RuntimeError(
                f"Bot {bot_id} has an unreadable evaluation activity claim"
            )
        return claim

    def _create_claim(self, bot_id: str, evaluation_id: str) -> None:
        path = self._claim_path(bot_id)
        payload = {
            "bot_id": bot_id,
            "evaluation_id": evaluation_id,
            "owner_pid": os.getpid(),
            "worker_pid": None,
            "created_at": _utc_now(),
        }
        encoded = (
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")
        try:
            descriptor = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError as exc:
            raise RuntimeError(
                f"Bot {bot_id} already has an evaluation activity claim"
            ) from exc
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
        except Exception:
            path.unlink(missing_ok=True)
            raise

    def _update_claim(
        self,
        bot_id: str,
        evaluation_id: str,
        *,
        worker_pid: int,
    ) -> None:
        claim = self._read_claim(bot_id)
        if claim is None or claim.get("evaluation_id") != evaluation_id:
            raise RuntimeError(
                f"Bot {bot_id} evaluation activity claim changed during startup"
            )
        claim["worker_pid"] = worker_pid
        _write_json(self._claim_path(bot_id), claim)

    def _release_claim(self, bot_id: str, evaluation_id: str) -> None:
        claim = self._read_claim(bot_id)
        if claim is None or claim.get("evaluation_id") != evaluation_id:
            return
        self._claim_path(bot_id).unlink(missing_ok=True)

    def _claim_is_live(
        self,
        claim: Mapping[str, Any],
        directory: Path,
    ) -> bool:
        worker_pid = claim.get("worker_pid")
        if isinstance(worker_pid, int):
            return self._worker_pid_status(worker_pid, directory) != "exited"
        owner_pid = claim.get("owner_pid")
        return isinstance(owner_pid, int) and self._pid_exists(owner_pid)

    def _remove_stale_orphan_claims(self) -> None:
        for path in self.root.glob(".active-*.json"):
            if path.is_symlink():
                raise RuntimeError(
                    f"evaluation activity claim cannot be a symlink: {path.name}"
                )
            claim = _read_json(path)
            bot_id = str(claim.get("bot_id") or "")
            evaluation_id = str(claim.get("evaluation_id") or "")
            if not bot_id or not evaluation_id:
                raise RuntimeError(
                    f"unreadable evaluation activity claim: {path.name}"
                )
            expected = self._claim_path(bot_id)
            if expected != path:
                raise RuntimeError(
                    f"invalid evaluation activity claim: {path.name}"
                )
            directory = self._evaluation_dir(evaluation_id)
            if not self._claim_is_live(claim, directory):
                self._release_claim(bot_id, evaluation_id)

    def _patch_report_status(
        self,
        evaluation_id: str,
        status: str,
        error: str,
    ) -> None:
        directory = self._evaluation_dir(evaluation_id)
        result_path = directory / "result.json"
        result = self._verified_result(
            evaluation_id,
            directory=directory,
            required=False,
        )
        if result:
            result["status"] = status
            result["finished_at"] = _utc_now()
            result["error"] = error
            _write_json(result_path, result)
        markdown_path = directory / "summary.md"
        self._verify_artifact_file(markdown_path, required=False)
        if markdown_path.is_file():
            text = markdown_path.read_text(encoding="utf-8")
            lines = text.splitlines()
            for index, line in enumerate(lines):
                if line.startswith(("- 状态：", "- status:")):
                    separator = "状态：" if line.startswith("- 状态：") else "status:"
                    lines[index] = f"- {separator} `{status}`"
                    break
            temporary = markdown_path.with_suffix(".md.tmp")
            temporary.write_text(
                "\n".join(lines) + "\n",
                encoding="utf-8",
            )
            temporary.replace(markdown_path)

    def _state(self, evaluation_id: str) -> dict[str, Any]:
        directory = self._verified_evaluation_dir(evaluation_id)
        state = _read_json(
            directory / "state.json"
        )
        return state

    def _verified_evaluation_dir(self, evaluation_id: str) -> Path:
        directory = self._evaluation_dir(evaluation_id)
        request_path = directory / "request.json"
        state_path = directory / "state.json"
        self._verify_artifact_file(request_path, required=True)
        self._verify_artifact_file(state_path, required=True)
        request = _read_json(request_path)
        state = _read_json(state_path)
        if not request or not state:
            raise KeyError(evaluation_id)
        if request.get("evaluation_id") != evaluation_id:
            raise ValueError(
                "evaluation_id does not match its request record"
            )
        if state.get("evaluation_id") != evaluation_id:
            raise ValueError(
                "evaluation_id does not match its state record"
            )
        return directory

    def _verified_result(
        self,
        evaluation_id: str,
        *,
        directory: Path | None = None,
        required: bool,
    ) -> dict[str, Any]:
        target = directory or self._verified_evaluation_dir(evaluation_id)
        path = target / "result.json"
        exists = self._verify_artifact_file(path, required=required)
        if not exists:
            return {}
        result = _read_json(path)
        if not result:
            raise ValueError("evaluation result is not valid JSON")
        if result.get("evaluation_id") != evaluation_id:
            raise ValueError(
                "evaluation_id does not match its result record"
            )
        return result

    @staticmethod
    def _verify_artifact_file(path: Path, *, required: bool) -> bool:
        if path.is_symlink():
            raise ValueError(
                f"evaluation artifact cannot be a symlink: {path.name}"
            )
        if not path.exists():
            if required:
                raise KeyError(path.name)
            return False
        if not path.is_file():
            raise ValueError(
                f"evaluation artifact is not a regular file: {path.name}"
            )
        return True

    def _evaluation_dir(self, evaluation_id: str) -> Path:
        if not evaluation_id or len(evaluation_id) > 128 or any(
            char
            not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
            for char in evaluation_id
        ):
            raise ValueError("invalid evaluation_id")
        path = self.root / evaluation_id
        if path.is_symlink():
            raise ValueError("evaluation directory cannot be a symlink")
        path.relative_to(self.root)
        return path

    @staticmethod
    def _selection_summary(request: Mapping[str, Any]) -> dict[str, Any]:
        kind = str(request.get("kind") or "")
        if kind == "comparison":
            cases = list(request.get("case_refs") or ())
            return {
                "kind": "profile",
                "id": str(request.get("profile_id") or ""),
                "preset": str(request.get("preset") or ""),
                "case_count": len(cases),
            }
        cases = list(request.get("case_ids") or ())
        return {
            "kind": "suite",
            "id": str(request.get("suite_id") or ""),
            "case_count": len(cases),
        }

    @staticmethod
    def _planned_trial_count(
        request: Mapping[str, Any],
        result: Mapping[str, Any],
    ) -> int:
        value = result.get("planned_trials")
        if isinstance(value, int):
            return value
        kind = str(request.get("kind") or "")
        if kind == "comparison":
            return (
                len(request.get("case_refs") or ())
                * int(request.get("repetitions") or 1)
                * len(request.get("target_ids") or request.get("targets") or ())
            )
        return len(request.get("case_ids") or ())

    @staticmethod
    def _clone_request(stored: Mapping[str, Any]) -> dict[str, Any]:
        kind = str(stored.get("kind") or "")
        if kind == "comparison":
            request: dict[str, Any] = {
                "kind": "comparison",
                "bot_id": str(stored.get("bot_id") or ""),
                "profile_id": str(stored.get("profile_id") or ""),
                "preset": str(stored.get("preset") or "quick"),
            }
            if request["preset"] == "custom":
                for key in (
                    "target_ids",
                    "case_refs",
                    "repetitions",
                    "max_wall_seconds",
                    "seed",
                ):
                    request[key] = stored.get(key)
            return request
        return {
            "kind": "suite",
            "bot_id": str(stored.get("bot_id") or ""),
            "suite_id": str(stored.get("suite_id") or ""),
            "case_ids": list(stored.get("case_ids") or ()),
            "dry_run": bool(stored.get("dry_run", False)),
            "llm_judge": bool(stored.get("llm_judge", False)),
        }

    @staticmethod
    def _pid_matches_evaluation(pid: int, directory: Path) -> bool:
        if pid <= 0:
            return False
        argv: list[str]
        if os.name == "nt":
            try:
                completed = subprocess.run(
                    [
                        "powershell",
                        "-NoProfile",
                        "-Command",
                        (
                            f"(Get-CimInstance Win32_Process -Filter "
                            f"\"ProcessId = {pid}\").CommandLine"
                        ),
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
            except (OSError, subprocess.SubprocessError):
                return False
            argv = EvaluationManager._split_windows_command_line(
                completed.stdout.strip()
            )
        else:
            try:
                raw_argv = Path(f"/proc/{pid}/cmdline").read_bytes()
            except OSError:
                return False
            argv = [
                value.decode("utf-8", errors="replace")
                for value in raw_argv.split(b"\0")
                if value
            ]
        return EvaluationManager._argv_matches_evaluation(argv, directory)

    @staticmethod
    def _argv_matches_evaluation(
        argv: Sequence[str],
        directory: Path,
    ) -> bool:
        normalized_tokens = {str(value).casefold() for value in argv}
        if "chatcopilot" not in normalized_tokens or "evals" not in normalized_tokens:
            return False
        output_values: list[str] = []
        index = 0
        while index < len(argv):
            value = str(argv[index])
            if value == "--output":
                if index + 1 >= len(argv):
                    return False
                output_values.append(str(argv[index + 1]))
                index += 2
                continue
            if value.startswith("--output="):
                output_values.append(value.partition("=")[2])
            index += 1
        if len(output_values) != 1:
            return False
        output = Path(output_values[0])
        if not output.is_absolute():
            return False
        try:
            actual = os.path.normcase(str(output.resolve(strict=False)))
            expected = os.path.normcase(str(directory.resolve(strict=False)))
        except OSError:
            return False
        return actual == expected

    @staticmethod
    def _split_windows_command_line(command_line: str) -> list[str]:
        argv: list[str] = []
        length = len(command_line)
        index = 0
        while index < length:
            while index < length and command_line[index] in " \t":
                index += 1
            if index >= length:
                break
            value: list[str] = []
            quoted = False
            while index < length:
                char = command_line[index]
                if char in " \t" and not quoted:
                    break
                if char == "\\":
                    start = index
                    while index < length and command_line[index] == "\\":
                        index += 1
                    slash_count = index - start
                    if index < length and command_line[index] == '"':
                        value.extend("\\" * (slash_count // 2))
                        if slash_count % 2:
                            value.append('"')
                            index += 1
                        elif (
                            quoted
                            and index + 1 < length
                            and command_line[index + 1] == '"'
                        ):
                            value.append('"')
                            index += 2
                        else:
                            quoted = not quoted
                            index += 1
                        continue
                    value.extend("\\" * slash_count)
                    continue
                if char == '"':
                    if (
                        quoted
                        and index + 1 < length
                        and command_line[index + 1] == '"'
                    ):
                        value.append('"')
                        index += 2
                    else:
                        quoted = not quoted
                        index += 1
                    continue
                value.append(char)
                index += 1
            argv.append("".join(value))
            while index < length and command_line[index] in " \t":
                index += 1
        return argv

    @classmethod
    def _worker_pid_status(
        cls,
        pid: int,
        directory: Path,
    ) -> WorkerPidStatus:
        if cls._pid_matches_evaluation(pid, directory):
            return "matched"
        if cls._pid_exists(pid):
            return "unknown"
        return "exited"

    @staticmethod
    def _pid_exists(pid: int) -> bool:
        if pid <= 0:
            return False
        if os.name == "nt":
            try:
                completed = subprocess.run(
                    [
                        "powershell",
                        "-NoProfile",
                        "-Command",
                        (
                            f"$p = Get-Process -Id {pid} "
                            "-ErrorAction SilentlyContinue; "
                            "if ($null -eq $p) { 'missing' } "
                            "else { 'present' }"
                        ),
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
            except (OSError, subprocess.SubprocessError):
                return True
            observation = completed.stdout.strip().lower()
            if observation == "missing":
                return False
            return True
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return True
        return True

    @staticmethod
    def _terminate_pid(pid: int) -> None:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                check=False,
                capture_output=True,
                text=True,
            )
            return
        try:
            os.killpg(pid, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            return

    @staticmethod
    def _terminate_process(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        if os.name == "nt":
            completed = subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                check=False,
                capture_output=True,
                text=True,
            )
            if completed.returncode != 0 and process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(
                    "evaluation process did not stop after termination"
                ) from exc
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(
                    "evaluation process did not stop after termination"
                ) from exc
        except ProcessLookupError:
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(
                    "evaluation process identity could not be confirmed"
                ) from exc


__all__ = [
    "ACTIVE_STATUSES",
    "EvaluationBlocked",
    "EvaluationManager",
]
