"""Opaque, production-disjoint Canary target handles."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import hmac
import os
from pathlib import Path
import re
import secrets
from typing import Iterable

from ._fs import (
    absolute_path,
    canonical_json,
    make_private_directory,
    paths_overlap,
    validate_private_directory,
)
from .errors import CanaryIntegrityError, CanarySafetyError


_SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_TARGET_ID = re.compile(r"^[0-9a-f]{32}$")


@dataclass(frozen=True, slots=True)
class ProductionFingerprint:
    """Trusted production identities that a Canary target must not overlap."""

    roots: tuple[Path, ...] = ()
    sockets: tuple[Path, ...] = ()
    units: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CanaryTargetHandle:
    schema_version: int
    evaluation_id: str
    trial_id: str
    template_id: str
    target_id: str
    unit_name: str
    private_root: Path
    target_root: Path
    source_base: Path
    source_work: Path
    releases_root: Path
    workspace_root: Path
    sockets_root: Path
    control_root: Path
    receipts_root: Path
    quarantine_root: Path
    seal: str


class CanaryTargetFactory:
    """Create and validate opaque target handles below one private root."""

    def __init__(
        self,
        private_root: str | os.PathLike[str],
        *,
        production_fingerprints: Iterable[ProductionFingerprint] = (),
        handle_key: bytes | None = None,
        expected_uid: int | None = None,
    ) -> None:
        self.private_root = absolute_path(private_root)
        self.expected_uid = os.getuid() if expected_uid is None else expected_uid
        validate_private_directory(self.private_root, expected_uid=self.expected_uid)
        resolved_root = self.private_root.resolve(strict=True)
        if resolved_root != self.private_root:
            raise CanarySafetyError("private Canary root must not traverse symlinks")
        self.production_fingerprints = tuple(production_fingerprints)
        self._production_paths = self._normalized_production_paths()
        self._production_units = {
            unit
            for item in self.production_fingerprints
            for unit in item.units
            if unit
        }
        self._reject_production_overlap(self.private_root)
        self._handle_key = handle_key or secrets.token_bytes(32)
        if len(self._handle_key) < 32:
            raise ValueError("Canary handle HMAC key must contain at least 32 bytes")

    def create_target(
        self,
        *,
        evaluation_id: str,
        trial_id: str,
        template_id: str,
    ) -> CanaryTargetHandle:
        _validate_external_id(evaluation_id, "evaluation_id")
        _validate_external_id(trial_id, "trial_id")
        _validate_external_id(template_id, "template_id")
        target_id = secrets.token_hex(16)
        target_root = self.private_root / target_id
        self._reject_production_overlap(target_root)
        make_private_directory(target_root, root=self.private_root, expected_uid=self.expected_uid)
        try:
            for relative in (
                "source",
                "source/base",
                "source/work",
                "runtime",
                "runtime/releases",
                "workspace",
                "sockets",
                "control",
                "receipts",
                "quarantine",
            ):
                path = target_root / relative
                make_private_directory(path, root=self.private_root, expected_uid=self.expected_uid)
        except Exception:
            # The caller owns cleanup of a partially provisioned target. Leaving it
            # visible is safer than recursively deleting an identity we cannot prove.
            raise
        handle = self._build_handle(
            evaluation_id=evaluation_id,
            trial_id=trial_id,
            template_id=template_id,
            target_id=target_id,
            seal="",
        )
        return self._build_handle(
            evaluation_id=evaluation_id,
            trial_id=trial_id,
            template_id=template_id,
            target_id=target_id,
            seal=self._sign(handle),
        )

    def validate_handle(self, handle: CanaryTargetHandle) -> CanaryTargetHandle:
        if handle.schema_version != 1 or not _TARGET_ID.fullmatch(handle.target_id):
            raise CanaryIntegrityError("Canary target handle identity is invalid")
        _validate_external_id(handle.evaluation_id, "evaluation_id")
        _validate_external_id(handle.trial_id, "trial_id")
        _validate_external_id(handle.template_id, "template_id")
        expected = self._build_handle(
            evaluation_id=handle.evaluation_id,
            trial_id=handle.trial_id,
            template_id=handle.template_id,
            target_id=handle.target_id,
            seal="",
        )
        expected_seal = self._sign(expected)
        if not hmac.compare_digest(handle.seal, expected_seal):
            raise CanaryIntegrityError("Canary target handle seal does not match")
        expected_signed = self._build_handle(
            evaluation_id=expected.evaluation_id,
            trial_id=expected.trial_id,
            template_id=expected.template_id,
            target_id=expected.target_id,
            seal=expected_seal,
        )
        if handle != expected_signed:
            raise CanaryIntegrityError("Canary target handle seal or paths were forged")
        if handle.unit_name in self._production_units:
            raise CanarySafetyError("Canary unit overlaps a production unit fingerprint")
        for path in _handle_directories(handle):
            self._reject_production_overlap(path)
            validate_private_directory(
                path,
                root=self.private_root,
                expected_uid=self.expected_uid,
            )
        return handle

    def _build_handle(
        self,
        *,
        evaluation_id: str,
        trial_id: str,
        template_id: str,
        target_id: str,
        seal: str,
    ) -> CanaryTargetHandle:
        target_root = self.private_root / target_id
        return CanaryTargetHandle(
            schema_version=1,
            evaluation_id=evaluation_id,
            trial_id=trial_id,
            template_id=template_id,
            target_id=target_id,
            unit_name=f"chatcopilot-canary@{target_id}.service",
            private_root=self.private_root,
            target_root=target_root,
            source_base=target_root / "source/base",
            source_work=target_root / "source/work",
            releases_root=target_root / "runtime/releases",
            workspace_root=target_root / "workspace",
            sockets_root=target_root / "sockets",
            control_root=target_root / "control",
            receipts_root=target_root / "receipts",
            quarantine_root=target_root / "quarantine",
            seal=seal,
        )

    def _sign(self, handle: CanaryTargetHandle) -> str:
        payload = asdict(handle)
        payload.pop("seal", None)
        for key, value in tuple(payload.items()):
            if isinstance(value, Path):
                payload[key] = os.fspath(value)
        return hmac.new(self._handle_key, canonical_json(payload), hashlib.sha256).hexdigest()

    def _normalized_production_paths(self) -> tuple[Path, ...]:
        paths: list[Path] = []
        for fingerprint in self.production_fingerprints:
            for raw in (*fingerprint.roots, *fingerprint.sockets):
                path = absolute_path(raw)
                paths.append(path.resolve(strict=False))
        return tuple(paths)

    def _reject_production_overlap(self, path: Path) -> None:
        canonical = path.resolve(strict=False)
        for production in self._production_paths:
            if paths_overlap(canonical, production):
                raise CanarySafetyError(
                    f"Canary path overlaps a production fingerprint: {canonical}"
                )


def _handle_directories(handle: CanaryTargetHandle) -> tuple[Path, ...]:
    return (
        handle.target_root,
        handle.source_base,
        handle.source_work,
        handle.releases_root,
        handle.workspace_root,
        handle.sockets_root,
        handle.control_root,
        handle.receipts_root,
        handle.quarantine_root,
    )


def _validate_external_id(value: str, name: str) -> None:
    if not _SAFE_ID.fullmatch(value):
        raise CanarySafetyError(f"{name} is not a safe stable identifier")


__all__ = ["CanaryTargetFactory", "CanaryTargetHandle", "ProductionFingerprint"]
