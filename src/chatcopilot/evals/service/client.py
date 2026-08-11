"""Synchronous same-user client for the local Evaluation service."""

from __future__ import annotations

import base64
import builtins
import os
import socket
import stat
import time
import uuid
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping

from chatcopilot.evals.service.protocol import (
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    MUTATION_ACCEPTED,
    MUTATION_OPERATIONS,
    PROTOCOL,
    ProtocolError,
    default_socket_path,
    recv_frame,
    send_frame,
)


class EvaluationServiceError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        checks: list[dict[str, Any]] | None = None,
    ) -> None:
        self.code = str(code or "internal_error")
        self.message = str(message or "Evaluation service request failed")
        self.checks = list(checks or ())
        super().__init__(self.message)


class EvaluationServiceUnavailable(EvaluationServiceError):
    def __init__(
        self,
        message: str,
        *,
        mutation_accepted: bool = False,
        operation: str = "",
        evaluation_id: str = "",
    ) -> None:
        self.mutation_accepted = mutation_accepted
        self.operation = operation
        self.evaluation_id = evaluation_id
        super().__init__("service_unavailable", message)


@dataclass(frozen=True)
class EvaluationReport:
    filename: str
    media_type: str
    content: bytes


@dataclass(frozen=True)
class EvaluationReportStream:
    filename: str
    media_type: str
    chunks: Iterator[bytes]


class EvaluationServiceClient:
    def __init__(
        self,
        socket_path: Path | None = None,
        *,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.socket_path = (socket_path or default_socket_path()).expanduser()
        self.timeout_seconds = float(timeout_seconds)

    def health(self) -> dict[str, Any]:
        return self._mapping(self._call("health", {}))

    def maintenance_status(self) -> dict[str, Any]:
        return self._mapping(self._call("maintenance.status", {}))

    def enter_maintenance(self, lease_id: str) -> dict[str, Any]:
        return self._mapping(
            self._call(
                "maintenance.enter",
                {"lease_id": lease_id},
            )
        )

    def leave_maintenance(self, lease_id: str) -> dict[str, Any]:
        return self._mapping(
            self._call(
                "maintenance.leave",
                {"lease_id": lease_id},
            )
        )

    def list_profiles(self) -> list[dict[str, Any]]:
        return self._mapping_list(self._call("profiles.list", {}))

    def list_suites(self, *, bot_id: str | None = None) -> list[dict[str, Any]]:
        return self._mapping_list(self._call("suites.list", {"bot_id": bot_id}))

    def prepare_suite(
        self,
        suite_id: str,
        *,
        bot_id: str | None = None,
    ) -> Iterator[str]:
        for item in self._stream(
            "suites.prepare",
            {"suite_id": suite_id, "bot_id": bot_id},
        ):
            yield str(item)

    def list_cases(
        self,
        suite_id: str,
        *,
        bot_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return self._mapping_list(
            self._call(
                "cases.list",
                {"suite_id": suite_id, "bot_id": bot_id},
            )
        )

    def get_case(
        self,
        suite_id: str,
        case_id: str,
        *,
        bot_id: str | None = None,
    ) -> dict[str, Any]:
        return self._mapping(
            self._call(
                "cases.get",
                {
                    "suite_id": suite_id,
                    "case_id": case_id,
                    "bot_id": bot_id,
                },
            )
        )

    def start(
        self,
        *,
        bot_id: str,
        request: Mapping[str, Any],
    ) -> dict[str, Any]:
        evaluation_id = self._new_evaluation_id()
        payload = {
            "bot_id": bot_id,
            "request": dict(request),
            "evaluation_id": evaluation_id,
        }
        return self._mapping(
            self._mutation_call(
                "evaluations.start",
                payload,
            )
        )

    def list(
        self,
        *,
        kind: str | None = None,
        bot_id: str | None = None,
        target: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        return self._mapping_list(
            self._call(
                "evaluations.list",
                {
                    "kind": kind,
                    "bot_id": bot_id,
                    "target": target,
                    "status": status,
                },
            )
        )

    def get(
        self,
        evaluation_id: str,
        *,
        include_result: bool = True,
    ) -> dict[str, Any]:
        return self._mapping(
            self._call(
                "evaluations.get",
                {
                    "evaluation_id": evaluation_id,
                    "include_result": include_result,
                },
            )
        )

    def case_detail(self, evaluation_id: str, case_ref: str) -> dict[str, Any]:
        return self._mapping(
            self._call(
                "evaluations.case",
                {"evaluation_id": evaluation_id, "case_ref": case_ref},
            )
        )

    def clone(self, evaluation_id: str) -> dict[str, Any]:
        new_evaluation_id = self._new_evaluation_id()
        return self._mapping(
            self._mutation_call(
                "evaluations.rerun",
                {
                    "source_evaluation_id": evaluation_id,
                    "evaluation_id": new_evaluation_id,
                },
            )
        )

    def cancel(self, evaluation_id: str) -> dict[str, Any]:
        return self._mapping(
            self._mutation_call(
                "evaluations.cancel",
                {"evaluation_id": evaluation_id},
            )
        )

    def delete(self, evaluation_id: str) -> None:
        self._mutation_call(
            "evaluations.delete",
            {"evaluation_id": evaluation_id},
        )

    def coverage(self, *, bot_id: str | None = None) -> dict[str, Any]:
        return self._mapping(self._call("coverage.list", {"bot_id": bot_id}))

    def follow(self, evaluation_id: str) -> Iterator[dict[str, Any] | str]:
        yield from self._stream(
            "evaluations.follow",
            {"evaluation_id": evaluation_id},
        )

    def report(self, evaluation_id: str, kind: str) -> EvaluationReport:
        stream = self.report_stream(evaluation_id, kind)
        return EvaluationReport(
            stream.filename,
            stream.media_type,
            b"".join(stream.chunks),
        )

    def report_stream(self, evaluation_id: str, kind: str) -> EvaluationReportStream:
        items = self._stream(
            "evaluations.report",
            {"evaluation_id": evaluation_id, "kind": kind},
        )
        try:
            metadata = next(items)
        except StopIteration as exc:
            raise EvaluationServiceError(
                "invalid_response",
                "Evaluation report stream has no metadata",
            ) from exc
        except Exception:
            items.close()
            raise
        if not isinstance(metadata, dict) or metadata.get("kind") != "metadata":
            items.close()
            raise EvaluationServiceError(
                "invalid_response",
                "Evaluation report stream returned invalid metadata",
            )
        filename = str(metadata.get("filename") or "")
        media_type = str(metadata.get("media_type") or "application/octet-stream")
        if not filename:
            items.close()
            raise EvaluationServiceError(
                "invalid_response",
                "Evaluation report stream has no filename",
            )

        def chunks() -> Iterator[bytes]:
            try:
                for item in items:
                    if not isinstance(item, dict) or item.get("kind") != "chunk":
                        raise EvaluationServiceError(
                            "invalid_response",
                            "Evaluation report stream returned an invalid chunk",
                        )
                    try:
                        yield base64.b64decode(
                            str(item.get("data") or ""),
                            validate=True,
                        )
                    except ValueError as exc:
                        raise EvaluationServiceError(
                            "invalid_response",
                            "Evaluation report stream contains invalid base64",
                        ) from exc
            finally:
                items.close()

        return EvaluationReportStream(filename, media_type, chunks())

    def _call(self, operation: str, payload: Mapping[str, Any]) -> Any:
        frames = self._exchange(operation, payload, streaming=False)
        try:
            return next(frames)
        except StopIteration as exc:
            raise EvaluationServiceError(
                "invalid_response",
                "Evaluation service returned no result",
            ) from exc
        finally:
            frames.close()

    def _mutation_call(self, operation: str, payload: Mapping[str, Any]) -> Any:
        try:
            return self._call(operation, payload)
        except EvaluationServiceUnavailable as exc:
            if not exc.mutation_accepted:
                raise
        deadline = time.monotonic() + max(self.timeout_seconds, 0.5)
        while True:
            try:
                return self._recover_mutation(operation, payload)
            except EvaluationServiceUnavailable as recovery_error:
                if time.monotonic() >= deadline:
                    evaluation_id = str(payload.get("evaluation_id") or "")
                    raise EvaluationServiceUnavailable(
                        "Evaluation mutation was accepted but recovery is unavailable; "
                        f"evaluation_id={evaluation_id}",
                        mutation_accepted=True,
                        operation=operation,
                        evaluation_id=evaluation_id,
                    ) from recovery_error
                time.sleep(0.05)

    def _recover_mutation(self, operation: str, payload: Mapping[str, Any]) -> Any:
        evaluation_id = str(payload.get("evaluation_id") or "")
        if operation in {"evaluations.start", "evaluations.rerun"}:
            try:
                return self.get(evaluation_id)
            except EvaluationServiceError as exc:
                if exc.code != "not_found":
                    raise
            return self._call(operation, payload)
        if operation == "evaluations.cancel":
            try:
                current = self.get(evaluation_id, include_result=False)
            except EvaluationServiceError as exc:
                if exc.code != "not_found":
                    raise
            else:
                if current.get("status") not in {"queued", "running"}:
                    return current
            return self._call(operation, payload)
        if operation == "evaluations.delete":
            try:
                self.get(evaluation_id, include_result=False)
            except EvaluationServiceError as exc:
                if exc.code == "not_found":
                    return {"ok": True}
                raise
            try:
                return self._call(operation, payload)
            except EvaluationServiceError as exc:
                if exc.code == "not_found":
                    return {"ok": True}
                raise
        raise EvaluationServiceError(
            "invalid_request",
            f"unsupported mutation recovery operation: {operation}",
        )

    def _stream(
        self,
        operation: str,
        payload: Mapping[str, Any],
    ) -> Generator[Any, None, None]:
        yield from self._exchange(operation, payload, streaming=True)

    def _exchange(
        self,
        operation: str,
        payload: Mapping[str, Any],
        *,
        streaming: bool,
    ) -> Generator[Any, None, None]:
        request_id = uuid.uuid4().hex
        connection: socket.socket | None = None
        mutation_accepted = False
        evaluation_id = str(payload.get("evaluation_id") or "")
        try:
            self._validate_socket()
            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            connection.settimeout(self.timeout_seconds)
            connection.connect(str(self.socket_path))
            if streaming:
                connection.settimeout(None)
        except (OSError, ValueError) as exc:
            if connection is not None:
                connection.close()
            raise EvaluationServiceUnavailable(f"Evaluation service is unavailable: {exc}") from exc
        assert connection is not None
        try:
            try:
                send_frame(
                    connection,
                    {
                        "protocol": PROTOCOL,
                        "request_id": request_id,
                        "operation": operation,
                        "payload": dict(payload),
                    },
                    max_bytes=MAX_REQUEST_BYTES,
                )
            except ProtocolError as exc:
                raise EvaluationServiceError(
                    "invalid_request",
                    "Evaluation service request exceeds the protocol limit",
                ) from exc
            while True:
                frame = recv_frame(connection, max_bytes=MAX_RESPONSE_BYTES)
                self._validate_response(frame, request_id=request_id)
                kind = str(frame.get("kind") or "")
                if kind == MUTATION_ACCEPTED:
                    data = frame.get("data")
                    accepted = data if isinstance(data, dict) else {}
                    if (
                        operation not in MUTATION_OPERATIONS
                        or mutation_accepted
                        or accepted.get("operation") != operation
                        or str(accepted.get("evaluation_id") or "") != evaluation_id
                    ):
                        raise EvaluationServiceError(
                            "invalid_response",
                            "Evaluation service returned invalid mutation acceptance",
                        )
                    mutation_accepted = True
                    connection.settimeout(None)
                    continue
                if kind == "error":
                    error = frame.get("error")
                    data = error if isinstance(error, dict) else {}
                    checks = data.get("checks")
                    raise EvaluationServiceError(
                        str(data.get("code") or "internal_error"),
                        str(data.get("message") or "Evaluation service request failed"),
                        checks=(
                            [dict(item) for item in checks if isinstance(item, dict)]
                            if isinstance(checks, list)
                            else []
                        ),
                    )
                if kind in {"result", "item"}:
                    if operation in MUTATION_OPERATIONS and not mutation_accepted:
                        raise EvaluationServiceError(
                            "invalid_response",
                            "Evaluation mutation was not accepted before its result",
                        )
                    yield frame.get("data")
                    if kind == "result":
                        return
                    continue
                if kind == "end":
                    return
                raise EvaluationServiceError(
                    "invalid_response",
                    "Evaluation service returned an unknown frame kind",
                )
        except (OSError, EOFError, ProtocolError) as exc:
            raise EvaluationServiceUnavailable(
                f"Evaluation service connection failed: {exc}",
                mutation_accepted=mutation_accepted,
                operation=operation,
                evaluation_id=evaluation_id,
            ) from exc
        finally:
            connection.close()

    def _validate_socket(self) -> None:
        path = self.socket_path
        if path.is_symlink():
            raise ValueError("Evaluation socket cannot be a symlink")
        metadata = path.stat()
        if not stat.S_ISSOCK(metadata.st_mode):
            raise ValueError("Evaluation socket path is not a Unix socket")
        parent = path.parent
        if parent.is_symlink():
            raise ValueError("Evaluation socket directory cannot be a symlink")
        parent_metadata = parent.stat()
        if not stat.S_ISDIR(parent_metadata.st_mode):
            raise ValueError("Evaluation socket parent is not a directory")
        if os.name != "nt" and hasattr(os, "getuid"):
            current_uid = os.getuid()
            if metadata.st_uid != current_uid or parent_metadata.st_uid != current_uid:
                raise ValueError("Evaluation socket must be owned by the current user")
            if stat.S_IMODE(metadata.st_mode) & 0o077:
                raise ValueError("Evaluation socket permissions are too broad")
            if stat.S_IMODE(parent_metadata.st_mode) & 0o077:
                raise ValueError("Evaluation socket directory permissions are too broad")

    @staticmethod
    def _validate_response(frame: Mapping[str, Any], *, request_id: str) -> None:
        if frame.get("protocol") != PROTOCOL:
            raise EvaluationServiceError(
                "invalid_response",
                "Evaluation service protocol version mismatch",
            )
        if frame.get("request_id") != request_id:
            raise EvaluationServiceError(
                "invalid_response",
                "Evaluation service response identity mismatch",
            )

    @staticmethod
    def _mapping(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise EvaluationServiceError(
                "invalid_response",
                "Evaluation service result must be an object",
            )
        return {str(key): item for key, item in value.items()}

    @staticmethod
    def _new_evaluation_id() -> str:
        return "eval-" + uuid.uuid4().hex

    @classmethod
    def _mapping_list(cls, value: Any) -> builtins.list[dict[str, Any]]:
        if not isinstance(value, list):
            raise EvaluationServiceError(
                "invalid_response",
                "Evaluation service result must be a list",
            )
        return [cls._mapping(item) for item in value]


__all__ = [
    "EvaluationReport",
    "EvaluationReportStream",
    "EvaluationServiceClient",
    "EvaluationServiceError",
    "EvaluationServiceUnavailable",
]
