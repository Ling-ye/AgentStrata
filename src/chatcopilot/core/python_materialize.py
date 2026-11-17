"""Materialize a pinned Python base image in a fresh private lab tree only.

This is an isolated staging operation, not installation, publication or runtime
acceptance. No archive program is executed. Failures retain their new private
stage for review; no existing object is repaired, replaced or deleted.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass
import hashlib
import io
import json
import os
from pathlib import Path
import tarfile
from typing import Protocol

from chatcopilot.core.install_artifacts import InstallError
from chatcopilot.core.isolated_files import IsolatedTree
from chatcopilot.core.python_plan import PythonPlanLimits, WindowsPythonPlan, windows_python_plan


class _PlannedFile(Protocol):
    @property
    def path(self) -> str: ...
    @property
    def size(self) -> int: ...
    @property
    def sha256(self) -> str: ...


class _TreePlan(Protocol):
    @property
    def files(self) -> Sequence[_PlannedFile]: ...
    @property
    def directories(self) -> tuple[str, ...]: ...


@dataclass(frozen=True)
class PythonMaterializeLimits:
    planning: PythonPlanLimits = PythonPlanLimits()
    max_files: int = 5000
    max_directories: int = 512
    max_total_bytes: int = 256 * 1024 * 1024
    max_handles: int = 6000
    handle_reserve: int = 64

    def __post_init__(self):
        if type(self.planning) is not PythonPlanLimits or any(type(value) is not int or value <= 0 for value in
                (self.max_files, self.max_directories, self.max_total_bytes, self.max_handles, self.handle_reserve)):
            raise InstallError("Isolated Python limits require positive integers and planning limits")


@dataclass(frozen=True)
class IsolatedPythonImage:
    stage_root: str
    relative_image_root: str
    plan_sha256: str
    inputs_sha256: str
    archive_sha256: str
    files: int
    directories: int
    bytes: int
    state: str = "staged"
    schema: str = "agentstrata-isolated-python-image/v1"


class IsolatedPythonError(InstallError):
    def __init__(self, *, stage_root: Path | None, cause: Exception):
        super().__init__("Isolated Python staging failed; retained workspace requires review")
        # A locator for operator review, never a deletion or execution capability.
        self.stage_root = str(stage_root) if stage_root is not None else None
        self.failure_type = type(cause).__name__


def _budget(plan: _TreePlan, workspace: Path, limits: PythonMaterializeLimits):
    handles = len(plan.files) + len(plan.directories) + len(workspace.parts) + 2
    if (len(plan.files) > limits.max_files or len(plan.directories) > limits.max_directories
            or sum(file.size for file in plan.files) > limits.max_total_bytes or handles > limits.max_handles):
        raise InstallError("Isolated Python plan exceeds materialization budgets")
    if os.name != "nt":
        import resource
        soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft != resource.RLIM_INFINITY and handles + limits.handle_reserve > soft:
            raise InstallError("Isolated Python staging requires more existing handle capacity")


def _plan(inputs, metadata, archive, expected_inputs_sha256, lane, workspace, limits):
    result = windows_python_plan(inputs, metadata, archive, expected_inputs_sha256=expected_inputs_sha256,
                                 lane=lane, limits=limits.planning)
    _budget(result, workspace, limits)
    return result


def _verify(tree: IsolatedTree, plan: _TreePlan):
    names = tuple(file.path for file in plan.files)
    tree.layout(plan.directories, names)
    stamps = {}
    for file in plan.files:
        ref = tree.nodes[file.path]
        before, size = tree._check(ref)
        if size != file.size:
            raise InstallError("Staged Python file size differs from the independent plan")
        tree.backend.rewind(ref)
        hashed, total = hashlib.sha256(), 0
        while chunk := tree.backend.read(ref, min(65536, file.size - total + 1)):
            total += len(chunk)
            if total > file.size:
                raise InstallError("Staged Python file exceeds the independent plan")
            hashed.update(chunk)
        after, final_size = tree._check(ref)
        if total != file.size or final_size != file.size or hashed.hexdigest() != file.sha256 or before != after:
            raise InstallError("Staged Python file content or identity changed")
        stamps[file.path] = after
    tree.layout(plan.directories, names)
    if any(tree._check(tree.nodes[name])[0] != stamp for name, stamp in stamps.items()):
        raise InstallError("Staged Python file metadata changed during verification")


def _evidence(tree: IsolatedTree, plan: WindowsPythonPlan):
    digest = hashlib.sha256(json.dumps(asdict(plan), sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return IsolatedPythonImage(str(tree.stage_path), plan.target_root, digest, plan.inputs_sha256, plan.pin.sha256,
                               len(plan.files), len(plan.directories), sum(file.size for file in plan.files))


def materialize_python_isolated(inputs: bytes, metadata: bytes, archive: bytes, *, expected_inputs_sha256: str,
                                lane: str, workspace: Path,
                                limits: PythonMaterializeLimits = PythonMaterializeLimits()) -> IsolatedPythonImage:
    """Create and verify new files beneath an existing empty private workspace.

    Original immutable inputs and the independently frozen host digest are checked
    before opening the workspace. No resolved plan, installed snapshot or target
    installation directory is accepted. The caller supplies a dedicated lab root.
    """
    workspace = Path(workspace)
    plan = _plan(inputs, metadata, archive, expected_inputs_sha256, lane, workspace, limits)
    tree = None
    try:
        tree = IsolatedTree(workspace, create=True)
        for path in plan.directories:
            tree.entry(path, directory=True, create=True)
        tree.layout(plan.directories, ())
        expected = {file.source_path: file for file in plan.files}
        source_directories = {plan.pin.root if path == plan.target_root else plan.pin.root + path[len(plan.target_root):]
                              for path in plan.directories if path == plan.target_root or path.startswith(plan.target_root + "/")}
        seen = set()
        # The strict immutable-byte pass already bounded and validated headers,
        # extensions, gzip completion and every payload. This independent parser
        # must agree with that plan before opening any corresponding new file.
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r|gz", encoding="ascii", errors="strict") as source:
            for item in source:
                if item.isdir():
                    if item.name.rstrip("/") not in source_directories:
                        raise InstallError("TAR reader directory interpretation differs from the strict plan")
                    continue
                if not item.isfile() or item.name not in expected or item.name in seen:
                    raise InstallError("TAR reader member interpretation differs from the strict plan")
                file = expected[item.name]
                if item.size != file.size:
                    raise InstallError("TAR reader size differs from the strict plan")
                seen.add(item.name)
                ref = tree.entry(file.path, directory=False, create=True)
                hashed, total = hashlib.sha256(), 0
                content = source.extractfile(item)
                if content is None:
                    raise InstallError("TAR reader omitted a regular file stream")
                with content:
                    while chunk := content.read(min(65536, file.size - total + 1)):
                        total += len(chunk)
                        if total > file.size:
                            raise InstallError("TAR reader content exceeds the strict plan")
                        hashed.update(chunk)
                        position = 0
                        while position < len(chunk):
                            written = tree.backend.write(ref, chunk[position:])
                            if not 0 < written <= len(chunk) - position:
                                raise InstallError("Isolated Python file write did not complete")
                            position += written
                if total != file.size or hashed.hexdigest() != file.sha256:
                    raise InstallError("TAR reader content differs from the strict plan")
                tree.backend.flush(ref)
        if seen != expected.keys():
            raise InstallError("TAR reader omitted a planned Python file")
        _verify(tree, plan)
        return _evidence(tree, plan)
    except Exception as error:
        raise IsolatedPythonError(stage_root=tree.stage_path if tree is not None else None, cause=error) from error
    finally:
        if tree is not None:
            tree.close()


def verify_python_isolated(inputs: bytes, metadata: bytes, archive: bytes, *, expected_inputs_sha256: str,
                           lane: str, stage_root: Path,
                           limits: PythonMaterializeLimits = PythonMaterializeLimits()) -> IsolatedPythonImage:
    """Recompute from independent inputs and read back a private staged tree.

    Local evidence JSON is never read. This is a held-handle observation, not an
    installed identity, process-start authorization or durable immutability claim.
    """
    stage_root = Path(stage_root)
    plan = _plan(inputs, metadata, archive, expected_inputs_sha256, lane, stage_root, limits)
    tree = None
    try:
        tree = IsolatedTree(stage_root, create=False)
        for path in plan.directories:
            tree.entry(path, directory=True, create=False)
        for file in plan.files:
            tree.entry(file.path, directory=False, create=False)
        _verify(tree, plan)
        return _evidence(tree, plan)
    except Exception as error:
        raise IsolatedPythonError(stage_root=stage_root, cause=error) from error
    finally:
        if tree is not None:
            tree.close()
