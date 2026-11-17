"""Stage a pinned Windows venv image; do not execute, publish or accept it."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import hashlib
import io
import os
from pathlib import Path
import tarfile
import zipfile

from chatcopilot.core.archive_inventory import ArchiveFile
from chatcopilot.core.install_artifacts import InstallError
from chatcopilot.core.isolated_files import IsolatedTree
from chatcopilot.core.python_environment import (
    BoundPythonEnvironment, EnvironmentFile, PythonEnvironmentLimits, WindowsPythonEnvironmentPlan,
    _bind, _budget_files, canonical_digest, windows_python_environment_plan,
)
from chatcopilot.core.python_materialize import _budget, _verify
from chatcopilot.core.python_plan import _digest


@dataclass(frozen=True)
class IsolatedPythonEnvironment:
    stage_root: str
    environment_root: str
    inputs_sha256: str
    plan_sha256: str
    filesystem_binding_sha256: str
    owner_sid: str
    python_archive_sha256: str
    wheel_sha256: tuple[str, ...]
    files: int
    directories: int
    bytes: int
    state: str = "staged"
    schema: str = "agentstrata-isolated-python-environment/v1"


class IsolatedEnvironmentError(InstallError):
    def __init__(self, *, stage_root, cause):
        super().__init__("Isolated environment staging failed; retained workspace requires review")
        self.stage_root = str(stage_root) if stage_root is not None else None
        self.failure_type = type(cause).__name__


@dataclass(frozen=True)
class _Budget:
    files: tuple[ArchiveFile, ...]
    directories: tuple[str, ...]


def _prepare(inputs, python_inputs, metadata, python_archive, wheel_archives, pin, workspace, limits):
    plan = windows_python_environment_plan(inputs, python_inputs, metadata, python_archive, wheel_archives,
                                            expected_inputs_sha256=pin, limits=limits)
    _budget(_Budget(_budget_files(plan), plan.directories), workspace, limits.python)
    if os.name != "nt":
        raise InstallError("Windows venv filesystem binding requires native Windows; pure planning remains portable")
    return plan


def _write(tree, refs, stream, size, expected):
    hashed, total = hashlib.sha256(), 0
    while chunk := stream.read(min(65536, size - total + 1)):
        total += len(chunk)
        if total > size:
            raise InstallError("Environment source stream exceeds its independently pinned member")
        hashed.update(chunk)
        for ref in refs:
            position = 0
            while position < len(chunk):
                written = tree.backend.write(ref, chunk[position:])
                if not 0 < written <= len(chunk) - position:
                    raise InstallError("Environment file write did not complete")
                position += written
    if total != size or hashed.hexdigest() != expected:
        raise InstallError("Environment source stream differs from its independently pinned member")
    for ref in refs:
        tree.backend.flush(ref)


def _python(tree, plan, bound, archive):
    expected: dict[str, list[EnvironmentFile]] = {}
    for file in bound.files:
        if file.source_kind == "python":
            if file.source_path is None:
                raise InstallError("Environment Python copy source is missing")
            expected.setdefault(file.source_path, []).append(file)
    source_directories = {plan.python.pin.root if path == plan.python.target_root else plan.python.pin.root + path[len(plan.python.target_root):]
        for path in plan.python.directories if path == plan.python.target_root or path.startswith(plan.python.target_root + "/")}
    seen = set()
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r|gz", encoding="ascii", errors="strict") as source:
        for item in source:
            if item.isdir():
                if item.name.rstrip("/") not in source_directories:
                    raise InstallError("Environment TAR reader directory disagrees with the strict plan")
                continue
            if not item.isfile() or item.name not in expected or item.name in seen:
                raise InstallError("Environment TAR reader member disagrees with the strict plan")
            files = expected[item.name]
            if any(file.size != item.size or file.sha256 != files[0].sha256 for file in files):
                raise InstallError("Environment TAR reader size disagrees with the strict plan")
            content = source.extractfile(item)
            if content is None:
                raise InstallError("Environment TAR reader omitted a regular stream")
            refs = [tree.entry(file.path, directory=False, create=True) for file in files]
            with content:
                _write(tree, refs, content, files[0].size, files[0].sha256)
            seen.add(item.name)
    if seen != expected.keys():
        raise InstallError("Environment TAR reader omitted a planned file")


def _wheels(tree, plan, bound, archives):
    by_hash = {hashlib.sha256(data).hexdigest(): data for data in archives}
    for wheel in plan.wheels.wheels:
        expected = {file.source_path: file for file in bound.files
                    if file.source_kind == "wheel" and file.input_sha256 == wheel.archive_sha256}
        with zipfile.ZipFile(io.BytesIO(by_hash[wheel.archive_sha256])) as source:
            names = [item.filename for item in source.infolist() if not item.is_dir()]
            if len(set(names)) != len(names) or set(names) != set(expected) | {wheel.replaced_record.path}:
                raise InstallError("Environment ZIP reader member set disagrees with the strict plan")
            for name, file in expected.items():
                info = source.getinfo(name)
                if info.filename != name or info.is_dir() or info.file_size != file.size:
                    raise InstallError("Environment ZIP reader identity disagrees with the strict plan")
                ref = tree.entry(file.path, directory=False, create=True)
                with source.open(info) as content:
                    _write(tree, [ref], content, file.size, file.sha256)
            record = wheel.replaced_record
            info = source.getinfo(record.path)
            if info.filename != record.path or info.is_dir() or info.file_size != record.size:
                raise InstallError("Environment ZIP reader original RECORD disagrees with the strict plan")
            with source.open(info) as content:
                _write(tree, [], content, record.size, record.sha256)


def _root(tree: IsolatedTree):
    if tree.root is None:
        raise InstallError("Environment private stage handle is unavailable")
    return tree.root


def _observed(tree: IsolatedTree, plan: WindowsPythonEnvironmentPlan, bound: BoundPythonEnvironment):
    import win32security
    from chatcopilot.core.windows_storage import _sid
    owner = win32security.ConvertSidToStringSid(_sid())
    binding = canonical_digest({"schema": "agentstrata-environment-filesystem-binding/v1",
        "bound_plan": asdict(bound), "owner_sid": owner,
        "storage_identities": tuple((name, ref.identity) for name, ref in sorted(tree.nodes.items()))})
    return IsolatedPythonEnvironment(str(tree.stage_path), plan.environment_root, plan.inputs_sha256,
        bound.plan_sha256, binding, owner, plan.python.pin.sha256, tuple(wheel.archive_sha256 for wheel in plan.wheels.wheels),
        len(bound.files), len(bound.directories), sum(file.size for file in bound.files))


def materialize_python_environment_isolated(inputs: bytes, python_inputs: bytes, metadata: bytes,
        python_archive: bytes, wheel_archives: tuple[bytes, ...], *, expected_inputs_sha256: str,
        workspace: Path, limits: PythonEnvironmentLimits = PythonEnvironmentLimits()) -> IsolatedPythonEnvironment:
    """Create a fresh private Windows lab tree from original independently pinned bytes.

    This invokes no interpreter, pip, venv command, package hook or shell script.
    Failure retains the new private tree. Nothing is replaced, moved or deleted.
    """
    workspace = Path(workspace)
    plan = _prepare(inputs, python_inputs, metadata, python_archive, wheel_archives, expected_inputs_sha256, workspace, limits)
    tree = None
    try:
        tree = IsolatedTree(workspace, create=True)
        bound = _bind(plan, _root(tree).path)
        for path in bound.directories:
            tree.entry(path, directory=True, create=True)
        tree.layout(bound.directories, ())
        _python(tree, plan, bound, python_archive)
        _wheels(tree, plan, bound, wheel_archives)
        for file in bound.files:
            if file.source_kind == "generated":
                if type(file.generated) is not bytes:
                    raise InstallError("Environment generated byte recipe is incomplete")
                ref = tree.entry(file.path, directory=False, create=True)
                _write(tree, [ref], io.BytesIO(file.generated), file.size, file.sha256)
        _verify(tree, bound)
        return _observed(tree, plan, bound)
    except Exception as error:
        raise IsolatedEnvironmentError(stage_root=tree.stage_path if tree is not None else None, cause=error) from error
    finally:
        if tree is not None:
            tree.close()


@contextmanager
def hold_python_environment_isolated(inputs: bytes, python_inputs: bytes, metadata: bytes,
        python_archive: bytes, wheel_archives: tuple[bytes, ...], *, expected_inputs_sha256: str,
        expected_filesystem_binding_sha256: str,
        stage_root: Path, limits: PythonEnvironmentLimits = PythonEnvironmentLimits()) -> Iterator[IsolatedPythonEnvironment]:
    """Hold verified image handles across a caller operation; recheck before releasing them.

    The result is observed staging evidence, never installed identity, acceptance,
    durable immutability, loader/ABI proof or authorization to start a process.
    """
    stage_root = Path(stage_root)
    plan = _prepare(inputs, python_inputs, metadata, python_archive, wheel_archives, expected_inputs_sha256, stage_root, limits)
    binding_pin = _digest(expected_filesystem_binding_sha256)
    tree = None
    try:
        try:
            tree = IsolatedTree(stage_root, create=False)
            bound = _bind(plan, _root(tree).path)
            for path in bound.directories:
                tree.entry(path, directory=True, create=False)
            for file in bound.files:
                tree.entry(file.path, directory=False, create=False)
            _verify(tree, bound)
            observed = _observed(tree, plan, bound)
            if observed.filesystem_binding_sha256 != binding_pin:
                raise InstallError("Environment filesystem binding differs from the caller's frozen observation")
        except Exception as error:
            raise IsolatedEnvironmentError(stage_root=stage_root, cause=error) from error
        # Caller failures are not validation failures; keep their type/cause intact.
        yield observed
        try:
            _verify(tree, bound)
            if _observed(tree, plan, bound) != observed:
                raise InstallError("Held environment identity changed during the caller operation")
        except Exception as error:
            raise IsolatedEnvironmentError(stage_root=stage_root, cause=error) from error
    finally:
        if tree is not None:
            tree.close()


def verify_python_environment_isolated(inputs: bytes, python_inputs: bytes, metadata: bytes,
        python_archive: bytes, wheel_archives: tuple[bytes, ...], *, expected_inputs_sha256: str,
        expected_filesystem_binding_sha256: str,
        stage_root: Path, limits: PythonEnvironmentLimits = PythonEnvironmentLimits()) -> IsolatedPythonEnvironment:
    """Recompute original inputs and anchored identity without installing or accepting code."""
    with hold_python_environment_isolated(inputs, python_inputs, metadata, python_archive, wheel_archives,
            expected_inputs_sha256=expected_inputs_sha256,
            expected_filesystem_binding_sha256=expected_filesystem_binding_sha256,
            stage_root=stage_root, limits=limits) as observed:
        return observed
