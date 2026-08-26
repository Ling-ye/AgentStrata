from __future__ import annotations

import asyncio
import base64
import json
import os
import signal
import socket
import stat
import struct
import subprocess
import sys
import textwrap
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from fastapi.responses import StreamingResponse
from starlette.requests import Request

from chatcopilot.evals.service import (
    EvaluationReportStream,
    EvaluationServiceClient,
    EvaluationServiceError,
    EvaluationServiceUnavailable,
)
from chatcopilot.evals.service.__main__ import main as evaluation_service_main
from chatcopilot.evals.service.protocol import (
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    PROTOCOL,
    recv_frame,
    send_frame,
)
from chatcopilot.evals.service.server import (
    EvaluationServiceRuntime,
    EvaluationUnixServer,
)
from chatcopilot.evals.application import catalog as evaluation_catalog
from chatcopilot.evals.application.bots import temporary_eval_env
from chatcopilot.evals.profiles import get_profile
from console.backend.app import app
from console.backend.routes.evaluations import export_evaluation


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
TERMINAL_STATUSES = {
    "completed",
    "partial",
    "cancelled",
    "interrupted",
    "error",
}


@dataclass(frozen=True)
class _ServiceHarness:
    socket_path: Path
    artifact_root: Path
    runtime: EvaluationServiceRuntime
    client: EvaluationServiceClient


def _write_completed_report(
    service: _ServiceHarness,
    evaluation_id: str,
    content: bytes,
) -> Path:
    directory = service.artifact_root / evaluation_id
    directory.mkdir(mode=0o700)
    artifacts = {
        "request.json": json.dumps(
            {
                "evaluation_id": evaluation_id,
                "bot_id": "lingye-copilot-qq",
                "kind": "suite",
                "created_at": "2026-08-11T00:00:00+00:00",
            }
        ).encode(),
        "state.json": json.dumps(
            {
                "evaluation_id": evaluation_id,
                "status": "completed",
            }
        ).encode(),
        "result.json": json.dumps(
            {
                "evaluation_id": evaluation_id,
                "status": "completed",
                "trials": [],
            }
        ).encode(),
        "summary.md": content,
    }
    for name, payload in artifacts.items():
        path = directory / name
        path.write_bytes(payload)
        if os.name != "nt":
            path.chmod(0o600)
    if os.name != "nt":
        directory.chmod(0o700)
    return directory / "summary.md"


@contextmanager
def _running_service(tmp_path: Path) -> Iterator[_ServiceHarness]:
    socket_path = tmp_path / "runtime" / "evaluation.sock"
    artifact_root = tmp_path / "artifacts"
    runtime = EvaluationServiceRuntime(
        repository_root=REPOSITORY_ROOT,
        artifact_root=artifact_root,
    )
    server = EvaluationUnixServer(socket_path, runtime)
    stopped = threading.Event()
    thread = threading.Thread(
        target=server.serve,
        args=(stopped,),
        name="test-evaluation-uds",
        daemon=True,
    )
    thread.start()
    client = EvaluationServiceClient(socket_path, timeout_seconds=5)
    try:
        assert client.health()["ready"] is True
        yield _ServiceHarness(
            socket_path=socket_path,
            artifact_root=artifact_root,
            runtime=runtime,
            client=client,
        )
    finally:
        for record in runtime.application.list():
            if record.get("status") in {"queued", "running"}:
                try:
                    runtime.application.cancel(str(record["evaluation_id"]))
                except (KeyError, RuntimeError, ValueError):
                    pass
        stopped.set()
        thread.join(timeout=3)
        server.close()
        assert not thread.is_alive()


def _wait_for_terminal(
    client: EvaluationServiceClient,
    evaluation_id: str,
    *,
    timeout_seconds: float = 20,
) -> dict:
    deadline = time.monotonic() + timeout_seconds
    latest: dict = {}
    while time.monotonic() < deadline:
        latest = client.get(evaluation_id)
        if latest.get("status") in TERMINAL_STATUSES:
            return latest
        time.sleep(0.05)
    pytest.fail(f"Evaluation {evaluation_id} did not reach a terminal state; latest={latest!r}")


def _start_service_process(
    socket_path: Path,
    artifact_root: Path,
) -> subprocess.Popen[bytes]:
    environment = os.environ.copy()
    source_root = str(REPOSITORY_ROOT / "src")
    environment["PYTHONPATH"] = os.pathsep.join(
        [
            source_root,
            *[item for item in environment.get("PYTHONPATH", "").split(os.pathsep) if item],
        ]
    )
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "chatcopilot.evals.service",
            "serve",
            "--socket",
            str(socket_path),
            "--repository-root",
            str(REPOSITORY_ROOT),
            "--artifact-root",
            str(artifact_root),
        ],
        cwd=REPOSITORY_ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _wait_for_service(
    process: subprocess.Popen[bytes],
    client: EvaluationServiceClient,
    *,
    timeout_seconds: float = 10,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.fail(f"Evaluation service exited with code {process.returncode}")
        try:
            if client.health().get("ready") is True:
                return
        except EvaluationServiceError as exc:
            last_error = exc
        time.sleep(0.05)
    pytest.fail(f"Evaluation service did not become ready: {last_error}")


def _stop_service_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _write_comparison_test_sitecustomize(tmp_path: Path) -> Path:
    injection_root = tmp_path / "process-injection"
    injection_root.mkdir()
    (injection_root / "sitecustomize.py").write_text(
        textwrap.dedent(
            """
            import time

            import chatcopilot.evals.evaluations as evaluation_module


            _parse_evaluation_request = evaluation_module.parse_evaluation_request


            def _target(target_id):
                backend = "codex" if target_id == "codex" else "native"
                fingerprint = ("c" if target_id == "codex" else "n") * 64
                return {
                    "target_id": target_id,
                    "label": target_id.title(),
                    "executor": "dry_run",
                    "backend": backend,
                    "model": "test-model",
                    "reasoning_effort": "test",
                    "fingerprint": fingerprint,
                    "config_fingerprint": "f" * 64,
                }


            def _validate(request):
                parsed = _parse_evaluation_request(request)
                return {
                    "ready": True,
                    "checks": [
                        {
                            "code": "test-process-injection",
                            "label": "Test process injection",
                            "ok": True,
                            "detail": "no external model execution",
                            "remediation": "",
                        }
                    ],
                    "effective_request": {
                        "kind": parsed.kind,
                        "bot": parsed.bot,
                        "profile": parsed.profile,
                        "preset": parsed.preset,
                        "targets": list(parsed.targets),
                        "case_refs": list(parsed.case_refs),
                        "repetitions": parsed.repetitions,
                        "max_wall_seconds": parsed.max_wall_seconds,
                        "seed": parsed.seed,
                    },
                    "targets": [_target(target_id) for target_id in parsed.targets],
                }


            def _execute(request):
                time.sleep(0.02)
                case_ref = request.profile_case.ref
                return evaluation_module.EvaluationTrial(
                    trial_id=(
                        f"{request.case.case_id}-a{request.attempt}-"
                        f"{request.target.target_id}"
                    ),
                    evaluation_id=request.evaluation_id,
                    kind=request.kind,
                    bot=request.bot,
                    profile=request.profile,
                    suite_id=request.suite_id,
                    case_ref=case_ref,
                    case_id=request.case.case_id,
                    dimension=request.dimension,
                    target_id=request.target.target_id,
                    target_fingerprint=request.target.fingerprint,
                    executor=request.target.executor,
                    backend=request.target.backend,
                    model=request.target.model,
                    reasoning_effort=request.target.reasoning_effort,
                    attempt=request.attempt,
                    order=request.order,
                    outcome="passed",
                    score=1.0,
                    max_score=1.0,
                    passed=True,
                    started_at="2026-08-11T00:00:00+00:00",
                    finished_at="2026-08-11T00:00:01+00:00",
                )


            evaluation_module.validate_evaluation = _validate
            evaluation_module.execute_evaluation_trial = _execute
            """
        ).lstrip(),
        encoding="utf-8",
    )
    return injection_root


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="Unix socket boundary")
def test_real_uds_health_and_private_socket_permissions(tmp_path: Path) -> None:
    with _running_service(tmp_path) as service:
        health = service.client.health()

        assert health == {
            "service": "agentstrata-evaluation",
            "schema_version": 1,
            "ready": True,
            "maintenance": False,
            "active_count": 0,
            "idle_proven": True,
        }
        assert stat.S_IMODE(service.socket_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(service.socket_path.parent.stat().st_mode) == 0o700

        alias = service.socket_path.parent / "alias.sock"
        alias.symlink_to(service.socket_path.name)
        with pytest.raises(EvaluationServiceUnavailable, match="cannot be a symlink"):
            EvaluationServiceClient(alias).health()

        service.socket_path.chmod(0o666)
        try:
            with pytest.raises(EvaluationServiceUnavailable, match="permissions are too broad"):
                service.client.health()
        finally:
            service.socket_path.chmod(0o600)


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="Unix socket boundary")
def test_mutation_acceptance_prevents_start_timeout_during_slow_prepare(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "CHATCOPILOT_LINGYE_API_KEY",
        "test-" + "credential",
    )
    monkeypatch.setenv("CHATCOPILOT_EVALS_DATA_DIR", str(tmp_path / "official-data"))
    prepare_entered = threading.Event()
    release_prepare = threading.Event()

    def slow_prepare(
        suite_id: str,
        _values: dict[str, str],
        _repository_root: Path,
    ) -> dict[str, object]:
        assert suite_id == "ifeval"
        with temporary_eval_env({}):
            prepare_entered.set()
            assert release_prepare.wait(timeout=5)
        return {"suite_id": suite_id, "ready": True}

    monkeypatch.setattr(evaluation_catalog, "_run_prepare_process", slow_prepare)
    with _running_service(tmp_path) as service:
        impatient = EvaluationServiceClient(service.socket_path, timeout_seconds=0.05)
        with ThreadPoolExecutor(max_workers=2) as executor:
            prepare = executor.submit(lambda: list(impatient.prepare_suite("ifeval")))
            assert prepare_entered.wait(timeout=2)
            started = executor.submit(
                impatient.start,
                bot_id="lingye-copilot-qq",
                request={
                    "kind": "suite",
                    "suite_id": "ifeval",
                    "case_ids": ["ifeval-json-format"],
                    "dry_run": True,
                    "llm_judge": False,
                },
            )
            time.sleep(0.15)
            assert not started.done()
            assert not list(service.artifact_root.glob("eval-*"))
            release_prepare.set()
            created = started.result(timeout=5)
            assert prepare.result(timeout=5)[-1] == "__EXIT__ 0"

        completed = _wait_for_terminal(
            service.client,
            str(created["evaluation_id"]),
        )
        assert completed["status"] == "completed"


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="Unix socket boundary")
def test_start_identity_is_idempotent_and_rejects_request_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed_id = "eval-idempotent-start"
    monkeypatch.setattr(
        EvaluationServiceClient,
        "_new_evaluation_id",
        staticmethod(lambda: fixed_id),
    )
    request = {
        "kind": "suite",
        "suite_id": "ifeval",
        "case_ids": ["ifeval-json-format"],
        "dry_run": True,
        "llm_judge": False,
    }
    with _running_service(tmp_path) as service:
        first = service.client.start(bot_id="lingye-copilot-qq", request=request)
        replay = service.client.start(bot_id="lingye-copilot-qq", request=request)

        assert first["evaluation_id"] == fixed_id
        assert replay["evaluation_id"] == fixed_id
        assert len(list(service.artifact_root.glob("eval-*"))) == 1
        with pytest.raises(EvaluationServiceError, match="different start request") as drift:
            service.client.start(
                bot_id="lingye-copilot-qq",
                request={**request, "llm_judge": True},
            )
        assert drift.value.code == "conflict"


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="Unix socket boundary")
def test_client_recovers_accepted_start_with_same_identity(tmp_path: Path) -> None:
    socket_parent = tmp_path / "runtime"
    socket_parent.mkdir(mode=0o700)
    socket_path = socket_parent / "recovery.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    socket_path.chmod(0o600)
    listener.listen(3)
    failure: list[BaseException] = []
    start_ids: list[str] = []

    def respond() -> None:
        try:
            connection, _ = listener.accept()
            with connection:
                frame = recv_frame(connection, max_bytes=MAX_REQUEST_BYTES)
                start_ids.append(str(frame["payload"]["evaluation_id"]))
                send_frame(
                    connection,
                    {
                        "protocol": PROTOCOL,
                        "request_id": frame["request_id"],
                        "kind": "mutation_accepted",
                        "data": {
                            "operation": "evaluations.start",
                            "evaluation_id": start_ids[-1],
                        },
                    },
                )

            connection, _ = listener.accept()
            with connection:
                frame = recv_frame(connection, max_bytes=MAX_REQUEST_BYTES)
                assert frame["operation"] == "evaluations.get"
                send_frame(
                    connection,
                    {
                        "protocol": PROTOCOL,
                        "request_id": frame["request_id"],
                        "kind": "error",
                        "error": {"code": "not_found", "message": "not found"},
                    },
                )

            connection, _ = listener.accept()
            with connection:
                frame = recv_frame(connection, max_bytes=MAX_REQUEST_BYTES)
                start_ids.append(str(frame["payload"]["evaluation_id"]))
                send_frame(
                    connection,
                    {
                        "protocol": PROTOCOL,
                        "request_id": frame["request_id"],
                        "kind": "mutation_accepted",
                        "data": {
                            "operation": "evaluations.start",
                            "evaluation_id": start_ids[-1],
                        },
                    },
                )
                send_frame(
                    connection,
                    {
                        "protocol": PROTOCOL,
                        "request_id": frame["request_id"],
                        "kind": "result",
                        "data": {"evaluation_id": start_ids[-1], "status": "running"},
                    },
                )
        except BaseException as exc:  # pragma: no cover - surfaced below
            failure.append(exc)

    thread = threading.Thread(target=respond, daemon=True)
    thread.start()
    try:
        created = EvaluationServiceClient(socket_path, timeout_seconds=1).start(
            bot_id="lingye-copilot-qq",
            request={"kind": "suite"},
        )
    finally:
        listener.close()
        thread.join(timeout=3)
        socket_path.unlink(missing_ok=True)

    assert not failure
    assert not thread.is_alive()
    assert len(start_ids) == 2
    assert start_ids[0] == start_ids[1] == created["evaluation_id"]


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="Unix socket boundary")
def test_first_delete_of_missing_evaluation_remains_not_found(tmp_path: Path) -> None:
    with _running_service(tmp_path) as service:
        with pytest.raises(EvaluationServiceError) as missing:
            service.client.delete("eval-never-created")
    assert missing.value.code == "not_found"


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="Unix socket boundary")
def test_persisted_maintenance_lease_blocks_creation_across_application_restart(
    tmp_path: Path,
) -> None:
    lease_id = "b" * 32
    with _running_service(tmp_path) as service:
        assert (
            evaluation_service_main(
                [
                    "maintenance",
                    "enter",
                    "--socket",
                    str(service.socket_path),
                    "--lease-id",
                    lease_id,
                ]
            )
            == 0
        )
        entered = service.client.maintenance_status()

        assert entered["maintenance"] is True
        assert entered["lease_id"] == lease_id
        assert service.client.maintenance_status()["lease_id"] == lease_id
        assert service.client.health()["maintenance"] is True
        marker = service.artifact_root / ".maintenance.json"
        marker_metadata = marker.lstat()
        assert stat.S_ISREG(marker_metadata.st_mode)
        if os.name != "nt":
            assert stat.S_IMODE(marker_metadata.st_mode) == 0o600

        with pytest.raises(EvaluationServiceError) as blocked:
            service.client.start(
                bot_id="lingye-copilot-qq",
                request={
                    "kind": "comparison",
                    "profile_id": "agent-comparison-mvp",
                    "preset": "quick",
                },
            )
        assert blocked.value.code == "conflict"
        assert "maintenance is active" in blocked.value.message
        assert not list(service.artifact_root.glob("eval-*"))

        restarted_application = service.runtime.application.__class__(
            service.artifact_root,
            repository_root=REPOSITORY_ROOT,
        )
        with pytest.raises(RuntimeError, match="maintenance is active"):
            restarted_application.start(
                bot_id="lingye-copilot-qq",
                request={
                    "kind": "comparison",
                    "profile_id": "agent-comparison-mvp",
                    "preset": "quick",
                },
            )

        with pytest.raises(EvaluationServiceError, match="does not match"):
            service.client.leave_maintenance("c" * 32)
        assert marker.is_file()
        assert (
            evaluation_service_main(
                [
                    "maintenance",
                    "leave",
                    "--socket",
                    str(service.socket_path),
                    "--lease-id",
                    lease_id,
                ]
            )
            == 0
        )
        assert service.client.health()["maintenance"] is False
        assert not marker.exists()


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="Unix socket boundary")
def test_health_does_not_read_large_or_invalid_historical_result(tmp_path: Path) -> None:
    with _running_service(tmp_path) as service:
        evaluation_id = "eval-health-history"
        directory = service.artifact_root / evaluation_id
        directory.mkdir(mode=0o700)
        request_path = directory / "request.json"
        request_path.write_text(
            json.dumps(
                {
                    "evaluation_id": evaluation_id,
                    "bot_id": "lingye-copilot-qq",
                }
            ),
            encoding="utf-8",
        )
        state_path = directory / "state.json"
        state_path.write_text(
            json.dumps(
                {
                    "evaluation_id": evaluation_id,
                    "status": "running",
                }
            ),
            encoding="utf-8",
        )
        result_path = directory / "result.json"
        result_path.write_bytes(b"not-json" + b"x" * (2 * 1024 * 1024))
        if os.name != "nt":
            request_path.chmod(0o600)
            state_path.chmod(0o600)
            result_path.chmod(0o600)

        started = time.monotonic()
        health = service.client.health()

        assert health["ready"] is True
        assert health["active_count"] == 1
        assert health["idle_proven"] is False
        assert time.monotonic() - started < 2
        assert (
            evaluation_service_main(
                [
                    "health",
                    "--socket",
                    str(service.socket_path),
                    "--require-idle",
                ]
            )
            == 2
        )

        state_path.write_text(
            json.dumps(
                {
                    "evaluation_id": evaluation_id,
                    "status": "completed",
                }
            ),
            encoding="utf-8",
        )
        if os.name != "nt":
            state_path.chmod(0o600)
        assert service.client.health()["idle_proven"] is True
        assert (
            evaluation_service_main(
                [
                    "health",
                    "--socket",
                    str(service.socket_path),
                    "--require-idle",
                ]
            )
            == 0
        )


@pytest.mark.parametrize(
    "state_content",
    [
        None,
        b"{not-json",
        json.dumps(
            {
                "evaluation_id": "eval-health-uncertain",
                "status": "future-status",
            }
        ).encode("utf-8"),
    ],
    ids=("missing", "invalid-json", "unknown-status"),
)
def test_health_cannot_prove_idle_from_invalid_lifecycle_state(
    tmp_path: Path,
    state_content: bytes | None,
) -> None:
    with _running_service(tmp_path) as service:
        directory = service.artifact_root / "eval-health-uncertain"
        directory.mkdir(mode=0o700)
        if state_content is not None:
            state_path = directory / "state.json"
            state_path.write_bytes(state_content)
            if os.name != "nt":
                state_path.chmod(0o600)

        health = service.client.health()

        assert health["ready"] is True
        assert health["active_count"] == 0
        assert health["idle_proven"] is False
        assert (
            evaluation_service_main(
                [
                    "health",
                    "--socket",
                    str(service.socket_path),
                    "--require-idle",
                ]
            )
            == 2
        )


def test_health_cli_requires_explicit_idle_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        EvaluationServiceClient,
        "health",
        lambda _self: {
            "ready": True,
            "active_count": 0,
        },
    )

    assert evaluation_service_main(["health", "--require-idle"]) == 2


def test_managed_worker_startup_gate_blocks_artifact_writes_until_released(
    tmp_path: Path,
) -> None:
    if os.name == "nt":
        pytest.skip("POSIX inherited descriptor boundary")
    evaluation_id = "eval-startup-gate"
    output = tmp_path / evaluation_id
    output.mkdir(mode=0o700)
    request_path = output / "request.json"
    request_path.write_text(
        json.dumps(
            {
                "evaluation_id": evaluation_id,
                "core_request": {"evaluation_id": evaluation_id},
            }
        ),
        encoding="utf-8",
    )
    request_path.chmod(0o600)
    reader, writer = os.pipe()
    os.set_inheritable(reader, True)
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        (
            str(REPOSITORY_ROOT / "src"),
            *(item for item in environment.get("PYTHONPATH", "").split(os.pathsep) if item),
        )
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "chatcopilot.evals.managed_worker",
            "--request",
            str(request_path),
            "--output",
            str(output),
            "--cancel-file",
            str(output / ".cancel-requested.json"),
            "--log-file",
            str(output / "run.log"),
            "--startup-fd",
            str(reader),
        ],
        cwd=REPOSITORY_ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        pass_fds=(reader,),
    )
    os.close(reader)
    try:
        time.sleep(0.15)
        assert process.poll() is None
        assert not (output / "run.log").exists()
        assert not (output / "result.json").exists()

        os.close(writer)
        writer = -1
        assert process.wait(timeout=5) == 2
        assert not (output / "run.log").exists()
        assert not (output / "result.json").exists()
    finally:
        if writer >= 0:
            os.close(writer)
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="Unix socket boundary")
def test_real_uds_rejects_oversized_and_wrong_protocol_frames(
    tmp_path: Path,
) -> None:
    with _running_service(tmp_path) as service:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.connect(str(service.socket_path))
            connection.sendall(struct.pack("!I", MAX_REQUEST_BYTES + 1))
            response = recv_frame(connection, max_bytes=MAX_RESPONSE_BYTES)

        assert response["kind"] == "error"
        assert response["error"]["code"] == "invalid_request"
        assert "frame size" in response["error"]["message"]

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.connect(str(service.socket_path))
            send_frame(
                connection,
                {
                    "protocol": "agentstrata.evaluation.v0",
                    "request_id": "wrong-version",
                    "operation": "health",
                    "payload": {},
                },
            )
            response = recv_frame(connection, max_bytes=MAX_RESPONSE_BYTES)

        assert response["kind"] == "error"
        assert response["error"]["code"] == "invalid_request"
        assert "version mismatch" in response["error"]["message"]

        with pytest.raises(EvaluationServiceError) as oversized:
            service.client.start(
                bot_id="lingye-copilot-qq",
                request={"kind": "suite", "padding": "x" * MAX_REQUEST_BYTES},
            )
        assert oversized.value.code == "invalid_request"
        assert "protocol limit" in oversized.value.message


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="Unix socket boundary")
def test_client_rejects_mismatched_response_identity(tmp_path: Path) -> None:
    socket_parent = tmp_path / "runtime"
    socket_parent.mkdir(mode=0o700)
    socket_path = socket_parent / "fake.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    socket_path.chmod(0o600)
    listener.listen(1)
    failure: list[BaseException] = []

    def respond_once() -> None:
        try:
            connection, _address = listener.accept()
            with connection:
                recv_frame(connection, max_bytes=MAX_REQUEST_BYTES)
                send_frame(
                    connection,
                    {
                        "protocol": PROTOCOL,
                        "request_id": "different-request",
                        "kind": "result",
                        "data": {"ready": True},
                    },
                )
        except BaseException as exc:  # pragma: no cover - surfaced below
            failure.append(exc)

    thread = threading.Thread(target=respond_once, daemon=True)
    thread.start()
    try:
        with pytest.raises(EvaluationServiceError) as raised:
            EvaluationServiceClient(socket_path).health()
    finally:
        listener.close()
        thread.join(timeout=3)
        socket_path.unlink(missing_ok=True)

    assert not failure
    assert not thread.is_alive()
    assert raised.value.code == "invalid_response"
    assert "identity mismatch" in raised.value.message


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="Unix socket boundary")
def test_real_uds_streams_large_report_in_multiple_chunks_and_through_console(
    tmp_path: Path,
) -> None:
    content = b"# Evaluation\n" + b"streaming-report-line\n" * 40000
    with _running_service(tmp_path) as service:
        evaluation_id = "eval-large-report"
        _write_completed_report(service, evaluation_id, content)

        report = service.client.report_stream(evaluation_id, "markdown")
        chunks = list(report.chunks)

        assert report.filename == "summary.md"
        assert report.media_type == "text/markdown"
        assert len(chunks) >= 3
        assert all(0 < len(chunk) <= 384 * 1024 for chunk in chunks)
        assert b"".join(chunks) == content

        previous_client = app.state.evaluations
        app.state.evaluations = service.client
        try:
            with TestClient(app) as console:
                response = console.get(f"/api/evals/evaluations/{evaluation_id}/export/markdown")
        finally:
            app.state.evaluations = previous_client

        assert response.status_code == 200
        assert response.content == content
        assert response.headers["content-type"].startswith("text/markdown")
        assert response.headers["content-disposition"] == ('attachment; filename="summary.md"')


@pytest.mark.skipif(
    os.name == "nt" or not hasattr(socket, "AF_UNIX"),
    reason="POSIX open-file deletion semantics",
)
def test_report_descriptor_survives_evaluation_deletion_after_metadata(
    tmp_path: Path,
) -> None:
    content = b"# Evaluation\n" + b"descriptor-owned-report\n" * 40000
    with _running_service(tmp_path) as service:
        evaluation_id = "eval-delete-after-metadata"
        report_path = _write_completed_report(service, evaluation_id, content)
        items = service.runtime.stream(
            "evaluations.report",
            {"evaluation_id": evaluation_id, "kind": "markdown"},
        )

        metadata = next(items)
        assert metadata == {
            "kind": "metadata",
            "filename": "summary.md",
            "media_type": "text/markdown",
        }

        service.runtime.dispatch(
            "evaluations.delete",
            {"evaluation_id": evaluation_id},
        )
        assert not report_path.exists()

        remaining = list(items)
        assert all(item.get("kind") == "chunk" for item in remaining)
        chunks = [base64.b64decode(str(item["data"]), validate=True) for item in remaining]
        assert len(chunks) >= 3
        assert b"".join(chunks) == content


def test_console_report_streaming_response_does_not_preconsume_chunks() -> None:
    consumed: list[str] = []

    class LazyReportClient:
        def report_stream(
            self,
            evaluation_id: str,
            kind: str,
        ) -> EvaluationReportStream:
            assert evaluation_id == "eval-lazy-report"
            assert kind == "markdown"

            def chunks() -> Iterator[bytes]:
                consumed.append("started")
                yield b"first-"
                consumed.append("continued")
                yield b"second"
                consumed.append("finished")

            return EvaluationReportStream(
                filename="summary.md",
                media_type="text/markdown",
                chunks=chunks(),
            )

    previous_client = app.state.evaluations
    app.state.evaluations = LazyReportClient()
    try:
        request = Request({"type": "http", "app": app})
        response = export_evaluation(
            request,
            "eval-lazy-report",
            "markdown",
        )
        assert isinstance(response, StreamingResponse)
        assert consumed == []

        async def collect_body() -> bytes:
            chunks = [chunk async for chunk in response.body_iterator]
            return b"".join(chunks)

        content = asyncio.run(collect_body())
    finally:
        app.state.evaluations = previous_client

    assert content == b"first-second"
    assert consumed == ["started", "continued", "finished"]


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="Unix socket boundary")
def test_real_ifeval_dry_run_lifecycle_over_uds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "CHATCOPILOT_LINGYE_API_KEY",
        "test-" + "credential",
    )
    monkeypatch.setenv(
        "CHATCOPILOT_EVALS_DATA_DIR",
        str(tmp_path / "official-data"),
    )
    for key in (
        "CHATCOPILOT_IFEVAL_DATA_PATH",
        "CHATCOPILOT_IFEVAL_MAX_CASES",
        "CHATCOPILOT_IFEVAL_CASE_PROFILE",
    ):
        monkeypatch.delenv(key, raising=False)

    with _running_service(tmp_path) as service:
        created = service.client.start(
            bot_id="lingye-copilot-qq",
            request={
                "kind": "suite",
                "bot_id": "lingye-copilot-qq",
                "suite_id": "ifeval",
                "case_ids": ["ifeval-json-format"],
                "dry_run": True,
                "llm_judge": False,
            },
        )
        evaluation_id = str(created["evaluation_id"])
        completed = _wait_for_terminal(service.client, evaluation_id)

        assert completed["status"] == "completed"
        assert completed["progress"] == {
            "completed": 1,
            "total": 1,
            "percent": 100,
        }
        assert completed["result"]["evaluation_id"] == evaluation_id
        assert completed["result"]["trials"][0]["outcome"] == "skipped"

        events = list(service.client.follow(evaluation_id))
        event_names = [event.get("event") for event in events if isinstance(event, dict)]
        assert event_names[0] == "evaluation_started"
        assert "trial_completed" in event_names
        assert event_names[-1] == "evaluation_status"

        json_stream = service.client.report_stream(evaluation_id, "json")
        markdown_report = service.client.report(evaluation_id, "markdown")
        assert json_stream.filename == "result.json"
        assert json.loads(b"".join(json_stream.chunks))["evaluation_id"] == evaluation_id
        assert markdown_report.filename == "summary.md"
        assert markdown_report.content.startswith(b"# Evaluation")

        directory = service.artifact_root / evaluation_id
        assert {
            "request.json",
            "state.json",
            "result.json",
            "summary.md",
            "progress.jsonl",
            "run.log",
        }.issubset({path.name for path in directory.iterdir()})
        if os.name != "nt":
            for path in directory.rglob("*"):
                mode = stat.S_IMODE(path.stat().st_mode)
                if path.is_dir():
                    assert mode == 0o700, path
                elif path.is_file():
                    assert mode == 0o600, path

        service.client.delete(evaluation_id)
        assert not directory.exists()
        with pytest.raises(EvaluationServiceError) as missing:
            service.client.get(evaluation_id)
        assert missing.value.code == "not_found"


@pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX") or not hasattr(signal, "SIGSTOP"),
    reason="POSIX process and Unix socket boundary",
)
def test_console_and_service_restart_preserve_and_recover_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "CHATCOPILOT_LINGYE_API_KEY",
        "test-" + "credential",
    )
    monkeypatch.setenv(
        "CHATCOPILOT_EVALS_DATA_DIR",
        str(tmp_path / "official-data"),
    )
    socket_path = tmp_path / "runtime" / "evaluation.sock"
    artifact_root = tmp_path / "artifacts"
    client = EvaluationServiceClient(socket_path, timeout_seconds=5)
    service_a = _start_service_process(socket_path, artifact_root)
    service_b: subprocess.Popen[bytes] | None = None
    worker_pid = 0
    worker_stopped = False
    previous_client = app.state.evaluations
    try:
        _wait_for_service(service_a, client)
        case_ids = ["ifeval-json-format"]
        created = client.start(
            bot_id="lingye-copilot-qq",
            request={
                "kind": "suite",
                "bot_id": "lingye-copilot-qq",
                "suite_id": "ifeval",
                "case_ids": case_ids,
                "dry_run": True,
                "llm_judge": False,
            },
        )
        evaluation_id = str(created["evaluation_id"])
        worker_pid = int(created["pid"])
        os.kill(worker_pid, signal.SIGSTOP)
        worker_stopped = True

        claims = list(artifact_root.glob(".active-*.json"))
        assert len(claims) == 1
        claim_metadata = claims[0].lstat()
        assert stat.S_ISREG(claim_metadata.st_mode)
        assert stat.S_IMODE(claim_metadata.st_mode) == 0o600
        assert json.loads(claims[0].read_text(encoding="utf-8"))["evaluation_id"] == evaluation_id

        app.state.evaluations = client
        with TestClient(app) as console_a:
            response = console_a.get(f"/api/evals/evaluations/{evaluation_id}")
            assert response.status_code == 200
            assert response.json()["status"] == "running"
            assert response.json()["pid"] == worker_pid
        os.kill(worker_pid, 0)
        assert client.get(evaluation_id, include_result=False)["status"] == "running"

        with TestClient(app) as console_b:
            response = console_b.get(f"/api/evals/evaluations/{evaluation_id}")
            assert response.status_code == 200
            assert response.json()["pid"] == worker_pid

        _stop_service_process(service_a)
        os.kill(worker_pid, 0)

        service_b = _start_service_process(socket_path, artifact_root)
        _wait_for_service(service_b, client)
        recovered = client.get(evaluation_id, include_result=False)
        assert recovered["status"] == "running"
        assert recovered["pid"] == worker_pid

        with TestClient(app) as console_after_service_restart:
            response = console_after_service_restart.get(f"/api/evals/evaluations/{evaluation_id}")
            assert response.status_code == 200
            assert response.json()["status"] == "running"
            assert response.json()["pid"] == worker_pid

        os.kill(worker_pid, signal.SIGCONT)
        worker_stopped = False
        completed = _wait_for_terminal(client, evaluation_id)
        assert completed["status"] == "completed"
        assert completed["progress"] == {
            "completed": len(case_ids),
            "total": len(case_ids),
            "percent": 100,
        }
        assert not list(artifact_root.glob(".active-*.json"))
    finally:
        app.state.evaluations = previous_client
        if worker_pid:
            if worker_stopped:
                try:
                    os.kill(worker_pid, signal.SIGCONT)
                except ProcessLookupError:
                    pass
            try:
                os.killpg(worker_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        _stop_service_process(service_a)
        if service_b is not None:
            _stop_service_process(service_b)


@pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX") or not hasattr(signal, "SIGSTOP"),
    reason="POSIX process and Unix socket boundary",
)
def test_real_managed_worker_cancels_at_a_complete_group_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    injection_root = _write_comparison_test_sitecustomize(tmp_path)
    monkeypatch.setenv("PYTHONPATH", str(injection_root))
    profile = get_profile("agent-comparison-mvp")
    case_ref = profile.cases[0].ref
    repetitions = 100

    socket_path = tmp_path / "runtime" / "evaluation.sock"
    artifact_root = tmp_path / "artifacts"
    client = EvaluationServiceClient(socket_path, timeout_seconds=10)
    service = _start_service_process(socket_path, artifact_root)
    worker_pid = 0
    worker_stopped = False
    try:
        _wait_for_service(service, client)
        created = client.start(
            bot_id="lingye-copilot-qq",
            request={
                "kind": "comparison",
                "bot_id": "lingye-copilot-qq",
                "profile_id": profile.profile_id,
                "preset": "custom",
                "target_ids": ["codex", "native"],
                "case_refs": [case_ref],
                "repetitions": repetitions,
                "max_wall_seconds": 30,
                "seed": 17,
            },
        )
        evaluation_id = str(created["evaluation_id"])
        worker_pid = int(created["pid"])
        progress_path = artifact_root / evaluation_id / "progress.jsonl"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if progress_path.is_file() and "target_group_completed" in progress_path.read_text(
                encoding="utf-8"
            ):
                break
            time.sleep(0.005)
        else:
            pytest.fail("managed worker did not complete its first target group")

        os.kill(worker_pid, signal.SIGSTOP)
        worker_stopped = True
        cancel_path = artifact_root / evaluation_id / ".cancel-requested.json"
        with ThreadPoolExecutor(max_workers=1) as executor:
            cancelled_future = executor.submit(client.cancel, evaluation_id)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not cancel_path.is_file():
                time.sleep(0.005)
            assert cancel_path.is_file()
            cancel_metadata = cancel_path.lstat()
            assert stat.S_ISREG(cancel_metadata.st_mode)
            assert stat.S_IMODE(cancel_metadata.st_mode) == 0o600
            assert (
                json.loads(cancel_path.read_text(encoding="utf-8"))["evaluation_id"]
                == evaluation_id
            )
            os.kill(worker_pid, signal.SIGCONT)
            worker_stopped = False
            cancelled = cancelled_future.result(timeout=10)

        assert cancelled["status"] == "cancelled"
        detail = client.get(evaluation_id)
        assert detail["status"] == "cancelled"
        assert detail["result"]["status"] == "cancelled"
        trials = detail["result"]["trials"]
        assert 0 < len(trials) < repetitions * 2
        targets_by_group: dict[tuple[str, int], set[str]] = {}
        for trial in trials:
            key = (str(trial["case_ref"]), int(trial["attempt"]))
            targets_by_group.setdefault(key, set()).add(str(trial["target_id"]))
        assert targets_by_group
        assert all(targets == {"codex", "native"} for targets in targets_by_group.values())
        assert not list(artifact_root.glob(".active-*.json"))
        assert not cancel_path.exists()
    finally:
        if worker_pid:
            if worker_stopped:
                try:
                    os.kill(worker_pid, signal.SIGCONT)
                except ProcessLookupError:
                    pass
            try:
                os.killpg(worker_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        _stop_service_process(service)


def test_console_bff_returns_503_without_local_fallback(tmp_path: Path) -> None:
    previous = app.state.evaluations
    missing_socket = tmp_path / "runtime" / "missing.sock"
    app.state.evaluations = EvaluationServiceClient(missing_socket)
    try:
        client = TestClient(app)
        health = client.get("/api/evals/health")
        records = client.get("/api/evals/evaluations")
    finally:
        app.state.evaluations = previous

    assert health.status_code == 503
    assert records.status_code == 503
    assert "Evaluation service is unavailable" in health.json()["detail"]
    assert "Evaluation service is unavailable" in records.json()["detail"]
    assert not missing_socket.parent.exists()


def test_console_bff_has_no_evaluation_process_or_artifact_owner() -> None:
    app_source = (REPOSITORY_ROOT / "console/backend/app.py").read_text(encoding="utf-8")
    route_sources = "\n".join(
        (REPOSITORY_ROOT / relative).read_text(encoding="utf-8")
        for relative in (
            "console/backend/routes/common.py",
            "console/backend/routes/evals.py",
            "console/backend/routes/evaluations.py",
        )
    )

    assert "EvaluationApplication" not in app_source
    assert "application.state.evaluations.close" not in app_source
    assert "console.control.evals" not in route_sources
    assert "console.control.evaluations" not in route_sources
    assert "chatcopilot.evals.application" not in route_sources
    assert "subprocess" not in route_sources
    assert "reports/evals/evaluations" not in route_sources
