"""Copy a rederived MCP layout into a fresh private lab; no executable prefix exists."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import io
from pathlib import Path
import shutil
import tarfile
import zipfile

from chatcopilot.core.install_artifacts import InstallError
from chatcopilot.core.isolated_files import IsolatedTree
from chatcopilot.core.mcp_project import MCPProjectLimits, WindowsMCPProjectPlan, windows_mcp_project_plan
from chatcopilot.core.python_environment import canonical_digest
from chatcopilot.core.python_environment_materialize import _write
from chatcopilot.core.python_materialize import PythonMaterializeLimits, _budget, _verify
from chatcopilot.core.wheel_plan import PinnedWheel


@dataclass(frozen=True)
class MCPCopyLimits:
    planning: MCPProjectLimits = MCPProjectLimits()
    storage: PythonMaterializeLimits = PythonMaterializeLimits(max_files=10000, max_directories=2000, max_handles=13000)
    min_free_bytes: int = 1024*1024*1024

    def __post_init__(self):
        if (type(self.planning) is not MCPProjectLimits or type(self.storage) is not PythonMaterializeLimits
                or type(self.min_free_bytes) is not int or self.min_free_bytes <= 0):
            raise InstallError("MCP isolated copy requires typed planning and finite storage limits")


@dataclass(frozen=True)
class IsolatedMCPLayout:
    stage_root: str
    plan_sha256: str
    source_archive_sha256: str
    files: int
    directories: int
    bytes: int
    state: str = "staged-data-only"
    schema: str = "agentstrata-isolated-mcp-layout/v1"
    executable_prefix_created: bool = False
    target_programs_executed: bool = False
    installation_accepted: bool = False


class IsolatedMCPError(InstallError):
    def __init__(self, tree, cause):
        super().__init__("MCP isolated data copy failed; retained private stage requires review")
        self.stage_root = str(tree.stage_path) if tree is not None else None
        self.failure_type = type(cause).__name__


def _source(tree, plan, raw):
    expected = {file.source_path:file for file in plan.files
        if file.input_sha256 == plan.source_archive_sha256 and file.source_path is not None}
    seen = set()
    # Planning authenticated these immutable bytes, then cross-checked the strict
    # TAR inventory against the streaming reader. No extraction API is used here.
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r|gz", encoding="ascii", errors="strict") as archive:
        for item in archive:
            if item.isdir():
                continue
            if not item.isfile():
                raise InstallError("MCP TAR reader disagrees with the independently authenticated source")
            if item.name not in expected:
                continue
            file = expected[item.name]
            if item.name in seen or item.size != file.size:
                raise InstallError("MCP source-copy member disagrees with the original plan")
            seen.add(item.name)
            content = archive.extractfile(item)
            if content is None:
                raise InstallError("MCP source-copy member stream is absent")
            ref = tree.entry(file.path,directory=False,create=True)
            with content:
                _write(tree,[ref],content,file.size,file.sha256)
    if seen != expected.keys():
        raise InstallError("MCP source reader omitted a planned member")


def _wheels(tree, plan, inputs):
    by_hash = {pin.sha256:pin.data for pin in inputs}
    for wheel in plan.wheels.wheels:
        expected = {file.source_path:file for file in plan.files
            if file.input_sha256 == wheel.archive_sha256 and file.source_path is not None}
        with zipfile.ZipFile(io.BytesIO(by_hash[wheel.archive_sha256])) as archive:
            names = [item.filename for item in archive.infolist() if not item.is_dir()]
            if len(set(names)) != len(names) or set(names) != set(expected) | {wheel.replaced_record.path}:
                raise InstallError("MCP ZIP reader member set disagrees with the original plan")
            for name,file in expected.items():
                item = archive.getinfo(name)
                if item.filename != name or item.is_dir() or item.file_size != file.size:
                    raise InstallError("MCP ZIP reader member identity disagrees with the original plan")
                ref = tree.entry(file.path,directory=False,create=True)
                with archive.open(item) as stream:
                    _write(tree,[ref],stream,file.size,file.sha256)
            record = wheel.replaced_record
            with archive.open(record.path) as stream:
                _write(tree,[],stream,record.size,record.sha256)


def _evidence(tree: IsolatedTree, plan: WindowsMCPProjectPlan):
    return IsolatedMCPLayout(str(tree.stage_path),canonical_digest(asdict(plan)),plan.source_archive_sha256,
        len(plan.files),len(plan.directories),sum(file.size for file in plan.files))


def _plan(source, wheels, workspace, limits, pins):
    if type(limits) is not MCPCopyLimits:
        raise InstallError("MCP isolated copy requires typed explicit limits")
    result = windows_mcp_project_plan(source,wheels,limits=limits.planning,**pins)
    _budget(result,workspace,limits.storage)
    return result


def materialize_mcp_layout_isolated(source: bytes, wheels: tuple[PinnedWheel,...], *, workspace: Path,
        limits: MCPCopyLimits = MCPCopyLimits(), **pins) -> IsolatedMCPLayout:
    """Create only new inert data under an empty private lab, retaining any failure.

    Every call recomputes the mapping from original TAR/wheel bytes and frozen
    source pins. No derived JSON, installed-state snapshot or accepted plan enters.
    """
    workspace = Path(workspace)
    plan = _plan(source,wheels,workspace,limits,pins)
    # This preflight retains headroom; it cannot reserve space against other writers.
    if shutil.disk_usage(workspace).free < sum(file.size for file in plan.files) + limits.min_free_bytes:
        raise InstallError("MCP isolated copy requires enough free space plus its configured headroom")
    tree = None
    try:
        tree = IsolatedTree(workspace,create=True)
        for path in plan.directories:
            tree.entry(path,directory=True,create=True)
        tree.layout(plan.directories,())
        _source(tree,plan,source)
        _wheels(tree,plan,wheels)
        for file in plan.files:
            if file.generated is not None:
                ref = tree.entry(file.path,directory=False,create=True)
                _write(tree,[ref],io.BytesIO(file.generated),file.size,file.sha256)
        _verify(tree,plan)
        return _evidence(tree,plan)
    except Exception as error:
        raise IsolatedMCPError(tree,error) from error
    finally:
        if tree is not None:
            tree.close()


def verify_mcp_layout_isolated(source: bytes, wheels: tuple[PinnedWheel,...], *, stage_root: Path,
        limits: MCPCopyLimits = MCPCopyLimits(), **pins) -> IsolatedMCPLayout:
    """Recompute original inputs and read every held file; never consume saved evidence."""
    stage_root = Path(stage_root)
    plan = _plan(source,wheels,stage_root,limits,pins)
    tree = None
    try:
        tree = IsolatedTree(stage_root,create=False)
        for path in plan.directories:
            tree.entry(path,directory=True,create=False)
        for file in plan.files:
            tree.entry(file.path,directory=False,create=False)
        _verify(tree,plan)
        return _evidence(tree,plan)
    except Exception as error:
        raise IsolatedMCPError(tree,error) from error
    finally:
        if tree is not None:
            tree.close()
