"""Parent-side integrity guard for authority-owned Evaluation artifacts.

The Evaluation Core owns the durable artifacts in an Evaluation directory.  A
Trial runs untrusted *task input* (and, eventually, a separately contained
plugin process), so the parent records the authority surface immediately before
the Trial and checks it again after every descendant has stopped.  This module
does not write any artifact; it only uses pinned directory descriptors and
``openat``-style operations to make an unexpected mutation a hard,
infrastructure-level failure.

It deliberately treats a suspicious filesystem state as an integrity failure,
rather than trying to recover or infer which process made the change.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import stat
from typing import Final, Mapping


_PRIVATE_DIRECTORY_MODE: Final = 0o700
_PRIVATE_FILE_MODE: Final = 0o600
_MAX_CANCEL_MARKER_BYTES: Final = 64 * 1024
_AUTHORITY_FILES: Final = (
    "request.json",
    "state.json",
    "result.json",
    "summary.md",
    "progress.jsonl",
)
_DEFAULT_CANCEL_MARKER: Final = ".cancel-requested.json"


class ArtifactIntegrityError(RuntimeError):
    """A Trial changed or made unsafe an authority-owned artifact."""

    code: Final[str] = "artifact_integrity_violation"

    def __init__(self, message: str, *, path: str | None = None) -> None:
        self.path = path
        self.details = {
            "code": self.code,
            "message": message,
            **({"path": path} if path else {}),
        }
        super().__init__(message)


@dataclass(frozen=True)
class ArtifactEntrySnapshot:
    """Stable metadata and content digest for one tracked authority entry."""

    exists: bool
    dev: int | None = None
    ino: int | None = None
    kind: str | None = None
    uid: int | None = None
    mode: int | None = None
    nlink: int | None = None
    size: int | None = None
    mtime_ns: int | None = None
    ctime_ns: int | None = None
    sha256: str | None = None


@dataclass(frozen=True)
class ArtifactIntegritySnapshot:
    """Immutable pre-Trial view of the authority artifact surface."""

    root: ArtifactEntrySnapshot
    files: Mapping[str, ArtifactEntrySnapshot]
    trials_directory: ArtifactEntrySnapshot
    trials: Mapping[str, ArtifactEntrySnapshot]
    cancel_marker: ArtifactEntrySnapshot
    claim: ArtifactEntrySnapshot | None


class ArtifactIntegrityGuard:
    """Pin and verify the authority surface for exactly one Trial.

    The guard intentionally owns open file descriptors.  Call :meth:`close`
    after verification, or use it as a context manager.  ``verify`` may be
    called more than once while the descriptors remain open.
    """

    def __init__(
        self,
        output: Path,
        *,
        evaluation_id: str,
        claim_path: Path | None = None,
        cancel_marker_name: str = _DEFAULT_CANCEL_MARKER,
    ) -> None:
        if not isinstance(evaluation_id, str) or not evaluation_id:
            raise ValueError("evaluation_id must be a non-empty string")
        if not _is_safe_single_name(cancel_marker_name):
            raise ValueError("cancel marker name must be one safe filename")

        self._output = _absolute(output)
        self._evaluation_id = evaluation_id
        self._cancel_marker_name = cancel_marker_name
        self._closed = False
        self._parent_fd = -1
        self._root_fd = -1
        self._claim_parent_fd = -1
        self._claim_name: str | None = None
        self._claim_path: Path | None = None
        self._claim_parent_path: Path | None = None
        self._claim_parent_initial: ArtifactEntrySnapshot | None = None

        try:
            # The Evaluation directory itself is always private.  A standalone
            # output may live below a user-owned, non-writable-by-others
            # repository directory (commonly mode 0755), so its parent is
            # pinned and checked without rewriting that broader directory's
            # permissions.
            self._parent_fd = _open_directory_path(
                self._output.parent,
                require_private_mode=False,
            )
            self._parent_initial = _owned_directory_snapshot_from_fd(
                self._parent_fd,
                self._output.parent,
            )
            self._root_fd = _open_directory_at(self._parent_fd, self._output.name)
            self._root_initial = _directory_snapshot_from_fd(self._root_fd, self._output)
            self._snapshot = self._capture_snapshot(claim_path)
        except BaseException:
            self.close()
            raise

    @classmethod
    def capture(
        cls,
        output: str | os.PathLike[str],
        *,
        evaluation_id: str,
        claim_path: str | os.PathLike[str] | None = None,
        cancel_marker_name: str = _DEFAULT_CANCEL_MARKER,
    ) -> "ArtifactIntegrityGuard":
        """Create a pinned, fully validated pre-Trial snapshot."""

        return cls(
            Path(output),
            evaluation_id=evaluation_id,
            claim_path=Path(claim_path) if claim_path is not None else None,
            cancel_marker_name=cancel_marker_name,
        )

    @property
    def snapshot(self) -> ArtifactIntegritySnapshot:
        return self._snapshot

    def verify(self) -> None:
        """Fail closed unless the authority surface still equals the snapshot.

        The only permitted transition is an absent cancellation marker becoming
        a new, private, canonical marker for this Evaluation.  A marker that
        already existed at capture time is frozen like every other artifact.
        """

        self._require_open()
        _assert_directory_identity(
            "Evaluation output parent directory",
            self._parent_initial,
            _snapshot_owned_directory_path(self._output.parent),
            self._output.parent,
        )
        _assert_named_directory_matches(
            self._parent_fd,
            self._output.name,
            self._root_initial,
            self._output,
        )
        # Adding the one permitted cancel marker necessarily changes the parent
        # directory's mtime/ctime.  Its own inode, ownership, mode, link count
        # and size are still frozen; the marker is separately validated below.
        _assert_directory_same_except_timestamps(
            "Evaluation output directory",
            self._snapshot.root,
            _directory_snapshot_from_fd(self._root_fd, self._output),
            self._output,
        )

        for name, expected in self._snapshot.files.items():
            _assert_same(
                f"authority artifact {name}",
                expected,
                _snapshot_file_at(self._root_fd, name, self._output / name),
                self._output / name,
            )

        current_trials_directory, current_trials = self._snapshot_trials()
        _assert_same(
            "trials directory",
            self._snapshot.trials_directory,
            current_trials_directory,
            self._output / "trials",
        )
        if set(current_trials) != set(self._snapshot.trials):
            raise _violation("trials artifact name set changed", self._output / "trials")
        for name, expected in self._snapshot.trials.items():
            _assert_same(
                f"trial artifact {name}",
                expected,
                current_trials[name],
                self._output / "trials" / name,
            )

        self._verify_cancel_marker()
        self._verify_claim()

    compare = verify

    def close(self) -> None:
        """Release the pinned directory descriptors; safe to call repeatedly."""

        if self._closed:
            return
        self._closed = True
        for attribute in ("_claim_parent_fd", "_root_fd", "_parent_fd"):
            descriptor = getattr(self, attribute, -1)
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                finally:
                    setattr(self, attribute, -1)

    def __enter__(self) -> "ArtifactIntegrityGuard":
        self._require_open()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _capture_snapshot(self, claim_path: Path | None) -> ArtifactIntegritySnapshot:
        files = {
            name: _snapshot_file_at(self._root_fd, name, self._output / name)
            for name in _AUTHORITY_FILES
        }
        trials_directory, trials = self._snapshot_trials()
        cancel_marker = _snapshot_file_at(
            self._root_fd,
            self._cancel_marker_name,
            self._output / self._cancel_marker_name,
        )
        if cancel_marker.exists:
            _validate_cancel_marker(
                self._root_fd,
                self._cancel_marker_name,
                self._output / self._cancel_marker_name,
                self._evaluation_id,
            )
        claim = self._capture_claim(claim_path)
        return ArtifactIntegritySnapshot(
            root=self._root_initial,
            files=files,
            trials_directory=trials_directory,
            trials=trials,
            cancel_marker=cancel_marker,
            claim=claim,
        )

    def _snapshot_trials(
        self,
    ) -> tuple[ArtifactEntrySnapshot, dict[str, ArtifactEntrySnapshot]]:
        directory_path = self._output / "trials"
        directory = _snapshot_directory_at(self._root_fd, "trials", directory_path)
        if not directory.exists:
            return directory, {}
        descriptor = _open_directory_at(self._root_fd, "trials")
        try:
            before = _directory_snapshot_from_fd(descriptor, directory_path)
            try:
                names = sorted(os.listdir(descriptor))
            except OSError as exc:
                raise _violation("unable to enumerate trials directory", directory_path) from exc
            if any(not _is_trial_name(name) for name in names):
                raise _violation("trials directory contains an unsafe entry", directory_path)
            entries = {
                name: _snapshot_file_at(descriptor, name, directory_path / name)
                for name in names
            }
            after = _directory_snapshot_from_fd(descriptor, directory_path)
        finally:
            os.close(descriptor)
        _assert_same("trials directory", before, after, directory_path)
        return before, entries

    def _capture_claim(self, claim_path: Path | None) -> ArtifactEntrySnapshot | None:
        if claim_path is None:
            return None
        path = _absolute(claim_path)
        self._claim_path = path
        self._claim_name = path.name
        self._claim_parent_path = path.parent
        self._claim_parent_fd = _open_directory_path(path.parent)
        self._claim_parent_initial = _directory_snapshot_from_fd(
            self._claim_parent_fd,
            path.parent,
        )
        return _snapshot_file_at(self._claim_parent_fd, path.name, path)

    def _verify_cancel_marker(self) -> None:
        current = _snapshot_file_at(
            self._root_fd,
            self._cancel_marker_name,
            self._output / self._cancel_marker_name,
        )
        expected = self._snapshot.cancel_marker
        if expected.exists:
            _assert_same("cancellation marker", expected, current, self._output / self._cancel_marker_name)
            return
        if not current.exists:
            return
        _validate_cancel_marker(
            self._root_fd,
            self._cancel_marker_name,
            self._output / self._cancel_marker_name,
            self._evaluation_id,
        )

    def _verify_claim(self) -> None:
        if self._snapshot.claim is None:
            return
        assert self._claim_name is not None
        assert self._claim_path is not None
        assert self._claim_parent_path is not None
        assert self._claim_parent_initial is not None
        # This is the shared managed Evaluation root.  A different Bot may
        # legitimately create or finish a sibling Evaluation while this Trial
        # runs, changing directory size/link/time metadata.  Freeze the
        # directory identity and security attributes, then freeze this claim
        # entry itself exactly.
        _assert_directory_identity(
            "Evaluation claim parent directory",
            self._claim_parent_initial,
            _snapshot_directory_path(self._claim_parent_path),
            self._claim_parent_path,
        )
        current = _snapshot_file_at(
            self._claim_parent_fd,
            self._claim_name,
            self._claim_path,
        )
        _assert_same("Evaluation claim", self._snapshot.claim, current, self._claim_path)

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("artifact integrity guard is closed")


def _absolute(path: Path) -> Path:
    if not path.is_absolute():
        path = Path(os.path.abspath(path))
    return Path(os.path.abspath(path))


def _open_directory_path(path: Path, *, require_private_mode: bool = True) -> int:
    _validate_directory_ancestors(path)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise _violation("authority directory cannot be opened safely", path) from exc
    try:
        metadata = os.fstat(descriptor)
        if require_private_mode:
            _validate_private_directory(metadata, path)
        else:
            _validate_owned_directory(metadata, path)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_directory_at(parent_fd: int, name: str) -> int:
    if not _is_safe_single_name(name):
        raise ValueError("directory entry must be one safe filename")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise _violation("authority directory cannot be opened safely", Path(name)) from exc


def _validate_directory_ancestors(path: Path) -> None:
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            metadata = os.lstat(current)
        except OSError as exc:
            raise _violation("authority directory ancestor is unavailable", current) from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise _violation("authority directory path contains a non-directory", current)


def _snapshot_directory_at(parent_fd: int, name: str, path: Path) -> ArtifactEntrySnapshot:
    try:
        metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return ArtifactEntrySnapshot(exists=False)
    except OSError as exc:
        raise _violation("authority directory cannot be inspected safely", path) from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise _violation("authority directory is not a real directory", path)
    descriptor = _open_directory_at(parent_fd, name)
    try:
        opened = _directory_snapshot_from_fd(descriptor, path)
    finally:
        os.close(descriptor)
    after = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if _metadata_identity(after) != _metadata_identity(metadata):
        raise _violation("authority directory changed while opening", path)
    return opened


def _snapshot_directory_path(path: Path) -> ArtifactEntrySnapshot:
    descriptor = _open_directory_path(path)
    try:
        return _directory_snapshot_from_fd(descriptor, path)
    finally:
        os.close(descriptor)


def _snapshot_owned_directory_path(path: Path) -> ArtifactEntrySnapshot:
    descriptor = _open_directory_path(path, require_private_mode=False)
    try:
        return _owned_directory_snapshot_from_fd(descriptor, path)
    finally:
        os.close(descriptor)


def _directory_snapshot_from_fd(descriptor: int, path: Path) -> ArtifactEntrySnapshot:
    metadata = os.fstat(descriptor)
    _validate_private_directory(metadata, path)
    return _metadata_snapshot(metadata, kind="directory")


def _owned_directory_snapshot_from_fd(
    descriptor: int,
    path: Path,
) -> ArtifactEntrySnapshot:
    metadata = os.fstat(descriptor)
    _validate_owned_directory(metadata, path)
    return _metadata_snapshot(metadata, kind="directory")


def _snapshot_file_at(parent_fd: int, name: str, path: Path) -> ArtifactEntrySnapshot:
    if not _is_safe_single_name(name):
        raise ValueError("artifact entry must be one safe filename")
    try:
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return ArtifactEntrySnapshot(exists=False)
    except OSError as exc:
        raise _violation("authority artifact cannot be inspected safely", path) from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise _violation("authority artifact is not a regular file", path)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise _violation("authority artifact cannot be opened safely", path) from exc
    try:
        opened = os.fstat(descriptor)
        _validate_private_file(opened, path)
        digest = _sha256_file(descriptor, path)
        final_metadata = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    try:
        after = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as exc:
        raise _violation("authority artifact changed while opening", path) from exc
    if (
        _metadata_identity(before) != _metadata_identity(opened)
        or _metadata_identity(after) != _metadata_identity(opened)
        or _metadata_identity(final_metadata) != _metadata_identity(opened)
    ):
        raise _violation("authority artifact changed while being read", path)
    return _metadata_snapshot(opened, kind="regular", digest=digest)


def _sha256_file(descriptor: int, path: Path) -> str:
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        digest = sha256()
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        return digest.hexdigest()
    except OSError as exc:
        raise _violation("authority artifact cannot be read safely", path) from exc


def _validate_private_directory(metadata: os.stat_result, path: Path) -> None:
    if not stat.S_ISDIR(metadata.st_mode):
        raise _violation("authority path is not a directory", path)
    if os.name != "nt":
        if metadata.st_uid != os.getuid():
            raise _violation("authority directory is not owned by the current user", path)
        if stat.S_IMODE(metadata.st_mode) != _PRIVATE_DIRECTORY_MODE:
            raise _violation("authority directory must use mode 0700", path)


def _validate_owned_directory(metadata: os.stat_result, path: Path) -> None:
    if not stat.S_ISDIR(metadata.st_mode):
        raise _violation("authority parent path is not a directory", path)
    if os.name != "nt":
        if metadata.st_uid != os.getuid():
            raise _violation("authority parent directory is not owned by the current user", path)
        if stat.S_IMODE(metadata.st_mode) & 0o022:
            raise _violation("authority parent directory is writable by another user", path)


def _validate_private_file(metadata: os.stat_result, path: Path) -> None:
    if not stat.S_ISREG(metadata.st_mode):
        raise _violation("authority artifact is not a regular file", path)
    if os.name != "nt":
        if metadata.st_uid != os.getuid():
            raise _violation("authority artifact is not owned by the current user", path)
        if stat.S_IMODE(metadata.st_mode) != _PRIVATE_FILE_MODE:
            raise _violation("authority artifact must use mode 0600", path)
        if metadata.st_nlink != 1:
            raise _violation("authority artifact must have exactly one hard link", path)


def _validate_cancel_marker(parent_fd: int, name: str, path: Path, evaluation_id: str) -> None:
    snapshot = _snapshot_file_at(parent_fd, name, path)
    if not snapshot.exists:
        raise _violation("cancellation marker disappeared during validation", path)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise _violation("cancellation marker cannot be opened safely", path) from exc
    try:
        raw = _read_all(descriptor, path)
    finally:
        os.close(descriptor)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _violation("cancellation marker is not valid JSON", path) from exc
    if not isinstance(payload, dict) or payload.get("evaluation_id") != evaluation_id:
        raise _violation("cancellation marker identity mismatch", path)
    try:
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8") + b"\n"
    except (TypeError, ValueError) as exc:
        raise _violation("cancellation marker is not canonical JSON", path) from exc
    if raw != canonical:
        raise _violation("cancellation marker is not canonical JSON", path)


def _read_all(descriptor: int, path: Path) -> bytes:
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > _MAX_CANCEL_MARKER_BYTES:
                raise _violation("cancellation marker exceeds the size limit", path)
        return b"".join(chunks)
    except OSError as exc:
        raise _violation("authority artifact cannot be read safely", path) from exc


def _metadata_snapshot(
    metadata: os.stat_result,
    *,
    kind: str,
    digest: str | None = None,
) -> ArtifactEntrySnapshot:
    return ArtifactEntrySnapshot(
        exists=True,
        dev=metadata.st_dev,
        ino=metadata.st_ino,
        kind=kind,
        uid=getattr(metadata, "st_uid", None),
        mode=stat.S_IMODE(metadata.st_mode),
        nlink=metadata.st_nlink,
        size=metadata.st_size,
        mtime_ns=metadata.st_mtime_ns,
        ctime_ns=metadata.st_ctime_ns,
        sha256=digest,
    )


def _metadata_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _assert_named_directory_matches(
    parent_fd: int,
    name: str,
    expected: ArtifactEntrySnapshot,
    path: Path,
) -> None:
    current = _snapshot_directory_at(parent_fd, name, path)
    _assert_directory_same_except_timestamps(
        "Evaluation output directory path",
        expected,
        current,
        path,
    )


def _assert_same(
    label: str,
    expected: ArtifactEntrySnapshot,
    current: ArtifactEntrySnapshot,
    path: Path,
) -> None:
    if expected != current:
        raise _violation(f"{label} changed during Trial execution", path)


def _assert_directory_same_except_timestamps(
    label: str,
    expected: ArtifactEntrySnapshot,
    current: ArtifactEntrySnapshot,
    path: Path,
) -> None:
    if (
        expected.exists != current.exists
        or expected.dev != current.dev
        or expected.ino != current.ino
        or expected.kind != current.kind
        or expected.uid != current.uid
        or expected.mode != current.mode
        or expected.nlink != current.nlink
        or expected.size != current.size
        or expected.sha256 != current.sha256
    ):
        raise _violation(f"{label} changed during Trial execution", path)


def _assert_directory_identity(
    label: str,
    expected: ArtifactEntrySnapshot,
    current: ArtifactEntrySnapshot,
    path: Path,
) -> None:
    if (
        expected.exists != current.exists
        or expected.dev != current.dev
        or expected.ino != current.ino
        or expected.kind != current.kind
        or expected.uid != current.uid
        or expected.mode != current.mode
    ):
        raise _violation(f"{label} changed during Trial execution", path)


def _is_safe_single_name(value: str) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value not in {".", ".."}
        and "/" not in value
        and "\\" not in value
        and "\x00" not in value
    )


def _is_trial_name(value: str) -> bool:
    return _is_safe_single_name(value) and value.endswith(".json") and not value.startswith(".")


def _violation(message: str, path: Path) -> ArtifactIntegrityError:
    return ArtifactIntegrityError(message, path=os.fspath(path))


__all__ = [
    "ArtifactEntrySnapshot",
    "ArtifactIntegrityError",
    "ArtifactIntegrityGuard",
    "ArtifactIntegritySnapshot",
]
