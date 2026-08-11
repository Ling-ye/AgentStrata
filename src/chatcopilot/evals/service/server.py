"""Same-user Unix socket server for the Evaluation application."""

from __future__ import annotations

import base64
import logging
import os
import socket
import socketserver
import stat
import struct
import threading
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from chatcopilot.evals.application import (
    EvaluationApplication,
    EvaluationBlocked,
    EvaluationBotRef,
    EvaluationBotResolver,
)
from chatcopilot.evals.application import catalog
from chatcopilot.evals.paths import managed_evaluation_root
from chatcopilot.evals.service.protocol import (
    MAX_REQUEST_BYTES,
    MUTATION_ACCEPTED,
    MUTATION_OPERATIONS,
    PROTOCOL,
    ProtocolError,
    recv_frame,
    send_frame,
)

LOGGER = logging.getLogger(__name__)
_REPORT_CHUNK_BYTES = 384 * 1024


class EvaluationServiceRuntime:
    def __init__(
        self,
        *,
        repository_root: Path,
        artifact_root: Path,
        application: EvaluationApplication | None = None,
        bot_resolver: EvaluationBotResolver | None = None,
    ) -> None:
        self.repository_root = repository_root.expanduser().resolve()
        self.bot_resolver = bot_resolver or EvaluationBotResolver(self.repository_root)
        self.application = application or EvaluationApplication(
            artifact_root,
            repository_root=self.repository_root,
            bot_resolver=self.bot_resolver,
        )

    def dispatch(self, operation: str, payload: Mapping[str, Any]) -> Any:
        if operation == "health":
            readiness = self.application.update_readiness()
            maintenance = self.application.maintenance_status()
            return {
                "service": "agentstrata-evaluation",
                "schema_version": 1,
                "ready": True,
                "maintenance": maintenance is not None,
                **readiness,
            }
        if operation == "maintenance.status":
            maintenance = self.application.maintenance_status()
            return maintenance or {"maintenance": False}
        if operation == "maintenance.enter":
            return self.application.enter_maintenance(_required_text(payload, "lease_id"))
        if operation == "maintenance.leave":
            return self.application.leave_maintenance(_required_text(payload, "lease_id"))
        if operation == "profiles.list":
            return catalog.list_profile_descriptors()
        if operation == "suites.list":
            return catalog.list_suite_descriptors(
                self._optional_bot(payload),
                repository_root=self.repository_root,
            )
        if operation == "cases.list":
            return catalog.list_case_summaries(
                _required_text(payload, "suite_id"),
                self._optional_bot(payload),
                repository_root=self.repository_root,
            )
        if operation == "cases.get":
            return catalog.get_case_descriptor(
                _required_text(payload, "suite_id"),
                _required_text(payload, "case_id"),
                self._optional_bot(payload),
                repository_root=self.repository_root,
            )
        if operation == "coverage.list":
            return self.application.coverage(bot_id=_optional_text(payload, "bot_id"))
        if operation == "evaluations.start":
            request = payload.get("request")
            if not isinstance(request, Mapping):
                raise ValueError("request must be a JSON object")
            return self.application.start(
                bot_id=_required_text(payload, "bot_id"),
                request={str(key): value for key, value in request.items()},
                evaluation_id=_required_text(payload, "evaluation_id"),
            )
        if operation == "evaluations.list":
            return self.application.list(
                kind=_optional_text(payload, "kind"),
                bot_id=_optional_text(payload, "bot_id"),
                target=_optional_text(payload, "target"),
                status=_optional_text(payload, "status"),
            )
        if operation == "evaluations.get":
            return self.application.get(
                _required_text(payload, "evaluation_id"),
                include_result=_optional_bool(
                    payload,
                    "include_result",
                    default=True,
                ),
            )
        if operation == "evaluations.case":
            return self.application.case_detail(
                _required_text(payload, "evaluation_id"),
                _required_text(payload, "case_ref"),
            )
        if operation == "evaluations.rerun":
            return self.application.clone(
                _required_text(payload, "source_evaluation_id"),
                new_evaluation_id=_required_text(payload, "evaluation_id"),
            )
        if operation == "evaluations.cancel":
            return self.application.cancel(_required_text(payload, "evaluation_id"))
        if operation == "evaluations.delete":
            self.application.delete(_required_text(payload, "evaluation_id"))
            return {"ok": True}
        raise ValueError(f"unsupported Evaluation service operation: {operation}")

    def stream(
        self,
        operation: str,
        payload: Mapping[str, Any],
    ) -> Iterator[Any]:
        if operation == "suites.prepare":
            yield from catalog.stream_prepare_suite(
                _required_text(payload, "suite_id"),
                self._optional_bot(payload),
                repository_root=self.repository_root,
            )
            return
        if operation == "evaluations.follow":
            yield from self.application.follow(_required_text(payload, "evaluation_id"))
            return
        if operation == "evaluations.report":
            yield from self._report_items(
                _required_text(payload, "evaluation_id"),
                _required_text(payload, "kind"),
            )
            return
        raise ValueError(f"unsupported Evaluation stream operation: {operation}")

    def _optional_bot(
        self,
        payload: Mapping[str, Any],
    ) -> EvaluationBotRef | None:
        bot_id = _optional_text(payload, "bot_id")
        return self.bot_resolver(bot_id) if bot_id else None

    def _report_items(
        self,
        evaluation_id: str,
        kind: str,
    ) -> Iterator[dict[str, str]]:
        path = self.application.report_path(evaluation_id, kind)
        media_type = "application/json" if kind == "json" else "text/markdown"
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("Evaluation report is not a regular file")
            if os.name != "nt":
                if metadata.st_uid != os.getuid():
                    raise PermissionError("Evaluation report must be owned by the service user")
                if stat.S_IMODE(metadata.st_mode) != 0o600:
                    raise PermissionError("Evaluation report must use mode 0600")
                if metadata.st_nlink != 1:
                    raise ValueError("Evaluation report must have exactly one hard link")
            with os.fdopen(descriptor, "rb") as handle:
                descriptor = -1
                yield {
                    "kind": "metadata",
                    "filename": path.name,
                    "media_type": media_type,
                }
                while True:
                    chunk = handle.read(_REPORT_CHUNK_BYTES)
                    if not chunk:
                        break
                    yield {
                        "kind": "chunk",
                        "data": base64.b64encode(chunk).decode("ascii"),
                    }
        finally:
            if descriptor >= 0:
                os.close(descriptor)


class _ThreadingUnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = False


class _RequestHandler(socketserver.BaseRequestHandler):
    server: _ThreadingUnixServer

    def handle(self) -> None:
        connection = self.request
        request_id = "unknown"
        try:
            _require_same_user(connection)
            frame = recv_frame(connection, max_bytes=MAX_REQUEST_BYTES)
            request_id, operation, payload = _parse_request(frame)
            runtime = getattr(self.server, "runtime")
            if operation in {
                "suites.prepare",
                "evaluations.follow",
                "evaluations.report",
            }:
                for item in runtime.stream(operation, payload):
                    _send_response(
                        connection,
                        request_id=request_id,
                        kind="item",
                        data=item,
                    )
                _send_response(
                    connection,
                    request_id=request_id,
                    kind="end",
                    data=None,
                )
            else:
                if operation in MUTATION_OPERATIONS:
                    _send_response(
                        connection,
                        request_id=request_id,
                        kind=MUTATION_ACCEPTED,
                        data=_mutation_acceptance(operation, payload),
                    )
                result = runtime.dispatch(operation, payload)
                _send_response(
                    connection,
                    request_id=request_id,
                    kind="result",
                    data=result,
                )
        except (BrokenPipeError, ConnectionResetError, EOFError):
            return
        except Exception as exc:  # noqa: BLE001 - service protocol boundary
            error = _error_payload(exc)
            if error["code"] == "internal_error":
                LOGGER.exception("Evaluation service request failed")
            try:
                send_frame(
                    connection,
                    {
                        "protocol": PROTOCOL,
                        "request_id": request_id,
                        "kind": "error",
                        "error": error,
                    },
                )
            except (OSError, ProtocolError):
                return


class EvaluationUnixServer:
    def __init__(
        self,
        socket_path: Path,
        runtime: EvaluationServiceRuntime,
    ) -> None:
        if not hasattr(socketserver, "UnixStreamServer"):
            raise RuntimeError("Evaluation service requires Unix sockets")
        self.socket_path = socket_path.expanduser()
        self._prepare_socket_path()
        self._server = _ThreadingUnixServer(
            str(self.socket_path),
            _RequestHandler,
        )
        setattr(self._server, "runtime", runtime)
        self.socket_path.chmod(0o600)
        self._socket_identity = self.socket_path.stat()

    def serve(self, stop_event: threading.Event) -> None:
        self._server.timeout = 0.2
        while not stop_event.is_set():
            self._server.handle_request()

    def close(self) -> None:
        self._server.server_close()
        try:
            metadata = self.socket_path.lstat()
        except FileNotFoundError:
            return
        if (
            stat.S_ISSOCK(metadata.st_mode)
            and metadata.st_uid == self._socket_identity.st_uid
            and metadata.st_ino == self._socket_identity.st_ino
        ):
            self.socket_path.unlink()

    def _prepare_socket_path(self) -> None:
        parent = self.socket_path.parent
        if parent.is_symlink():
            raise ValueError("Evaluation socket directory cannot be a symlink")
        parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        if not parent.is_dir():
            raise ValueError("Evaluation socket parent must be a directory")
        if os.name != "nt" and hasattr(os, "getuid"):
            metadata = parent.stat()
            if metadata.st_uid != os.getuid():
                raise PermissionError(
                    "Evaluation socket directory must be owned by the service user"
                )
            parent.chmod(0o700)
        try:
            metadata = self.socket_path.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISSOCK(metadata.st_mode):
            raise ValueError("Evaluation socket path must not replace another inode")
        if os.name != "nt" and metadata.st_uid != os.getuid():
            raise PermissionError("stale Evaluation socket has a foreign owner")
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(0.2)
        try:
            probe.connect(str(self.socket_path))
        except OSError:
            self.socket_path.unlink()
        else:
            raise RuntimeError("Evaluation service socket is already active")
        finally:
            probe.close()


def _parse_request(
    frame: Mapping[str, Any],
) -> tuple[str, str, dict[str, Any]]:
    if frame.get("protocol") != PROTOCOL:
        raise ProtocolError("Evaluation service protocol version mismatch")
    request_id = str(frame.get("request_id") or "").strip()
    if not request_id or len(request_id) > 128:
        raise ProtocolError("Evaluation service request_id is invalid")
    operation = str(frame.get("operation") or "").strip()
    if not operation or len(operation) > 128:
        raise ProtocolError("Evaluation service operation is invalid")
    payload = frame.get("payload")
    if not isinstance(payload, Mapping):
        raise ProtocolError("Evaluation service payload must be an object")
    return request_id, operation, {str(key): value for key, value in payload.items()}


def _send_response(
    connection: socket.socket,
    *,
    request_id: str,
    kind: str,
    data: Any,
) -> None:
    send_frame(
        connection,
        {
            "protocol": PROTOCOL,
            "request_id": request_id,
            "kind": kind,
            "data": data,
        },
    )


def _mutation_acceptance(
    operation: str,
    payload: Mapping[str, Any],
) -> dict[str, str]:
    return {
        "operation": operation,
        "evaluation_id": _required_text(payload, "evaluation_id"),
    }


def _error_payload(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, EvaluationBlocked):
        return {
            "code": str(exc.payload.get("code") or "evaluation_blocked"),
            "message": str(exc.payload.get("message") or str(exc)),
            "checks": list(exc.payload.get("checks") or ()),
        }
    if isinstance(exc, KeyError):
        return {"code": "not_found", "message": "Evaluation resource not found"}
    if isinstance(exc, RuntimeError):
        return {"code": "conflict", "message": str(exc)}
    if isinstance(exc, (ProtocolError, ValueError, OSError)):
        return {"code": "invalid_request", "message": str(exc)}
    return {"code": "internal_error", "message": "Evaluation service failed"}


def _required_text(payload: Mapping[str, Any], key: str) -> str:
    value = str(payload.get(key) or "").strip()
    if not value:
        raise ValueError(f"{key} is required")
    return value


def _optional_text(payload: Mapping[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_bool(
    payload: Mapping[str, Any],
    key: str,
    *,
    default: bool,
) -> bool:
    value = payload.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be boolean")
    return value


def _require_same_user(connection: socket.socket) -> None:
    if not hasattr(socket, "SO_PEERCRED") or not hasattr(os, "getuid"):
        return
    size = struct.calcsize("3i")
    credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, size)
    _pid, peer_uid, _gid = struct.unpack("3i", credentials)
    if peer_uid != os.getuid():
        raise PermissionError("Evaluation service peer uid is not authorized")


def build_runtime(
    *,
    repository_root: Path,
    artifact_root: Path | None = None,
) -> EvaluationServiceRuntime:
    repository = repository_root.expanduser().resolve()
    root = artifact_root or managed_evaluation_root(repository)
    return EvaluationServiceRuntime(
        repository_root=repository,
        artifact_root=root,
    )


__all__ = [
    "EvaluationServiceRuntime",
    "EvaluationUnixServer",
    "build_runtime",
]
