"""Immutable local generation staging and guarded activation descriptors."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import stat
from typing import Mapping

from ._fs import (
    atomic_write_private_json,
    ensure_contained,
    make_private_directory,
    read_private_json,
    validate_private_directory,
    validate_private_file,
    write_new_private_file,
)
from .errors import CanaryConflictError, CanaryIntegrityError, CanarySafetyError
from .target import CanaryTargetFactory, CanaryTargetHandle


_GENERATION_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_MANIFEST_NAME = ".generation.json"


@dataclass(frozen=True, slots=True)
class GenerationDescriptor:
    generation_id: str
    digest: str
    root: Path
    files: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ActivationDescriptor:
    target_id: str
    active_generation: str
    generation_digest: str
    previous_generation: str | None


class GenerationStore:
    """Stage immutable generations and atomically select one for a target."""

    def __init__(
        self,
        factory: CanaryTargetFactory,
        handle: CanaryTargetHandle,
    ) -> None:
        self.factory = factory
        self.handle = factory.validate_handle(handle)
        self.activation_path = self.handle.target_root / "runtime/activation.json"

    def stage(
        self,
        generation_id: str,
        files: Mapping[str, bytes | str],
    ) -> GenerationDescriptor:
        self._validate_generation_id(generation_id)
        if not files:
            raise CanarySafetyError("a Canary generation must contain at least one file")
        destination = self.handle.releases_root / generation_id
        ensure_contained(destination, self.handle.private_root)
        if destination.exists() or destination.is_symlink():
            raise CanaryConflictError(f"Canary generation already exists: {generation_id}")
        staging = self.handle.releases_root / f".stage-{generation_id}-{secrets.token_hex(8)}"
        make_private_directory(staging, root=self.handle.private_root)
        normalized: dict[str, bytes] = {}
        try:
            for raw_name, raw_data in files.items():
                relative = _safe_relative_path(raw_name)
                name = relative.as_posix()
                if name == _MANIFEST_NAME:
                    raise CanarySafetyError(f"reserved Canary generation path: {name}")
                data = raw_data.encode("utf-8") if isinstance(raw_data, str) else bytes(raw_data)
                if name in normalized:
                    raise CanarySafetyError(f"duplicate Canary generation path: {name}")
                normalized[name] = data
            for name in sorted(normalized):
                relative = PurePosixPath(name)
                parent = staging
                for part in relative.parts[:-1]:
                    candidate = parent / part
                    if not candidate.exists():
                        make_private_directory(candidate, root=self.handle.private_root)
                    else:
                        validate_private_directory(candidate, root=self.handle.private_root)
                    parent = candidate
                write_new_private_file(
                    staging.joinpath(*relative.parts),
                    normalized[name],
                    root=self.handle.private_root,
                )
            digest = _generation_digest(normalized)
            manifest = {
                "schema_version": 1,
                "target_id": self.handle.target_id,
                "generation_id": generation_id,
                "generation_digest": digest,
                "files": {
                    name: {
                        "sha256": hashlib.sha256(data).hexdigest(),
                        "size": len(data),
                    }
                    for name, data in sorted(normalized.items())
                },
            }
            atomic_write_private_json(
                staging / _MANIFEST_NAME,
                manifest,
                root=self.handle.private_root,
            )
            os.rename(staging, destination)
            _fsync_directory(self.handle.releases_root)
        except Exception:
            if staging.exists():
                _remove_staging_directory(staging, releases_root=self.handle.releases_root)
            raise
        return self.verify_generation(generation_id)

    def verify_generation(self, generation_id: str) -> GenerationDescriptor:
        self.factory.validate_handle(self.handle)
        self._validate_generation_id(generation_id)
        root = self.handle.releases_root / generation_id
        validate_private_directory(root, root=self.handle.private_root)
        manifest_path = root / _MANIFEST_NAME
        manifest = read_private_json(manifest_path, root=self.handle.private_root)
        if (
            manifest.get("schema_version") != 1
            or manifest.get("target_id") != self.handle.target_id
            or manifest.get("generation_id") != generation_id
        ):
            raise CanaryIntegrityError("Canary generation manifest identity is invalid")
        expected_files = manifest.get("files")
        if not isinstance(expected_files, dict) or not expected_files:
            raise CanaryIntegrityError("Canary generation manifest has no files")
        observed: dict[str, bytes] = {}
        for directory, directory_names, file_names in os.walk(root, followlinks=False):
            current = Path(directory)
            validate_private_directory(current, root=self.handle.private_root)
            for name in directory_names:
                candidate = current / name
                info = os.lstat(candidate)
                if stat.S_ISLNK(info.st_mode):
                    raise CanarySafetyError("Canary generation contains a symlink directory")
            for name in file_names:
                candidate = current / name
                validate_private_file(candidate, root=self.handle.private_root)
                relative = candidate.relative_to(root).as_posix()
                if relative == _MANIFEST_NAME:
                    continue
                observed[relative] = candidate.read_bytes()
        if set(observed) != set(expected_files):
            raise CanaryIntegrityError("Canary generation file set does not match its manifest")
        for name, data in observed.items():
            entry = expected_files.get(name)
            if not isinstance(entry, dict):
                raise CanaryIntegrityError("Canary generation manifest entry is invalid")
            if entry.get("size") != len(data) or not hmac_digest_matches(
                str(entry.get("sha256") or ""), hashlib.sha256(data).hexdigest()
            ):
                raise CanaryIntegrityError(f"Canary generation file drifted: {name}")
        digest = _generation_digest(observed)
        if not hmac_digest_matches(str(manifest.get("generation_digest") or ""), digest):
            raise CanaryIntegrityError("Canary generation digest does not match")
        return GenerationDescriptor(
            generation_id=generation_id,
            digest=digest,
            root=root,
            files=tuple(sorted(observed)),
        )

    def activate(
        self,
        generation_id: str,
        *,
        expected_current: str | None = None,
    ) -> ActivationDescriptor:
        generation = self.verify_generation(generation_id)
        current = self.current_activation()
        current_id = current.active_generation if current else None
        if current_id != expected_current:
            raise CanaryConflictError(
                f"active Canary generation drifted: expected {expected_current!r}, got {current_id!r}"
            )
        payload = {
            "schema_version": 1,
            "target_id": self.handle.target_id,
            "active_generation": generation.generation_id,
            "generation_digest": generation.digest,
            "previous_generation": current_id,
        }
        atomic_write_private_json(
            self.activation_path,
            payload,
            root=self.handle.private_root,
        )
        return self.current_activation(required=True)

    def restore(
        self,
        baseline_generation: str,
        *,
        expected_active_generation: str,
    ) -> ActivationDescriptor:
        return self.activate(
            baseline_generation,
            expected_current=expected_active_generation,
        )

    def current_activation(self, *, required: bool = False) -> ActivationDescriptor | None:
        if not self.activation_path.exists() and not self.activation_path.is_symlink():
            if required:
                raise CanaryIntegrityError("Canary activation descriptor is missing")
            return None
        payload = read_private_json(self.activation_path, root=self.handle.private_root)
        generation_id = str(payload.get("active_generation") or "")
        if payload.get("schema_version") != 1 or payload.get("target_id") != self.handle.target_id:
            raise CanaryIntegrityError("Canary activation identity is invalid")
        generation = self.verify_generation(generation_id)
        if not hmac_digest_matches(
            str(payload.get("generation_digest") or ""), generation.digest
        ):
            raise CanaryIntegrityError("active Canary generation digest drifted")
        previous = payload.get("previous_generation")
        if previous is not None and not isinstance(previous, str):
            raise CanaryIntegrityError("Canary previous generation is invalid")
        return ActivationDescriptor(
            target_id=self.handle.target_id,
            active_generation=generation_id,
            generation_digest=generation.digest,
            previous_generation=previous,
        )

    @staticmethod
    def _validate_generation_id(value: str) -> None:
        if not _GENERATION_ID.fullmatch(value):
            raise CanarySafetyError("Canary generation_id is invalid")


def _safe_relative_path(value: str) -> PurePosixPath:
    if not value or "\\" in value:
        raise CanarySafetyError(f"invalid Canary generation path: {value!r}")
    path = PurePosixPath(value)
    if (
        path == PurePosixPath(".")
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise CanarySafetyError(f"invalid Canary generation path: {value!r}")
    return path


def _generation_digest(files: Mapping[str, bytes]) -> str:
    digest = hashlib.sha256()
    for name, data in sorted(files.items()):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(len(data)).encode("ascii"))
        digest.update(b"\0")
        digest.update(data)
        digest.update(b"\0")
    return digest.hexdigest()


def hmac_digest_matches(left: str, right: str) -> bool:
    # compare_digest is also appropriate for unsigned SHA-256 values and avoids
    # accidental timing-sensitive comparisons when this primitive is reused.
    import hmac

    return hmac.compare_digest(left, right)


def _remove_staging_directory(path: Path, *, releases_root: Path) -> None:
    ensure_contained(path, releases_root)
    if not path.name.startswith(".stage-"):
        raise CanarySafetyError("refusing to remove a non-staging Canary directory")
    shutil.rmtree(path)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = ["ActivationDescriptor", "GenerationDescriptor", "GenerationStore"]
