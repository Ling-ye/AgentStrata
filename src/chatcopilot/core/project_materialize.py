"""Stage rederived project bytes in held private files, without executable prefixes."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import io
from pathlib import Path
import shutil

from chatcopilot.core.install_artifacts import InstallError
from chatcopilot.core.isolated_files import IsolatedTree
from chatcopilot.core.project_patch import RECIPE, ProjectFile, ProjectPatchLimits, project_source_mapping
from chatcopilot.core.python_environment import canonical_digest
from chatcopilot.core.python_environment_materialize import _write
from chatcopilot.core.python_materialize import PythonMaterializeLimits, _budget, _verify


@dataclass(frozen=True)
class ProjectSourceInput:
    source: bytes
    patch: bytes
    expected_archive_sha256: str
    expected_revision: str
    expected_patch_sha256: str
    source_root: str
    target_root: str
    allowed_cjk_paths: tuple[str, ...] = ()
    recipe: str = RECIPE


@dataclass(frozen=True)
class ProjectCopyLimits:
    planning: ProjectPatchLimits = ProjectPatchLimits()
    storage: PythonMaterializeLimits = PythonMaterializeLimits(max_files=4000,max_directories=1000,
        max_total_bytes=256*1024*1024,max_handles=6000)
    min_free_bytes: int = 1024*1024*1024

    def __post_init__(self):
        if (type(self.planning) is not ProjectPatchLimits or type(self.storage) is not PythonMaterializeLimits
                or type(self.min_free_bytes) is not int or self.min_free_bytes<=0):
            raise InstallError("Project isolated copy requires explicit finite planning and storage limits")


@dataclass(frozen=True)
class _CombinedPlan:
    files: tuple[ProjectFile, ...]
    directories: tuple[str, ...]


@dataclass(frozen=True)
class IsolatedProjectSources:
    stage_root: str
    plan_sha256: str
    files: int
    directories: int
    bytes: int
    schema: str = "agentstrata-isolated-plugin-project-sources/v1"
    state: str = "staged-source-data-only"
    target_programs_executed: bool = False
    executable_prefix_created: bool = False
    installation_accepted: bool = False


class IsolatedProjectError(InstallError):
    def __init__(self,tree,cause):
        super().__init__("Isolated project staging failed; retained private stage requires review")
        self.stage_root = str(tree.stage_path) if tree is not None else None
        self.failure_type = type(cause).__name__


def _plan(inputs,workspace,limits):
    if (type(limits) is not ProjectCopyLimits or type(inputs) is not tuple or not 0<len(inputs)<=2
            or any(type(item) is not ProjectSourceInput for item in inputs)):
        raise InstallError("Project staging requires one or two original typed inputs")
    plans = []
    contents: dict[str, bytes] = {}
    for item in inputs:
        pins = {key:value for key,value in vars(item).items() if key not in {'source','patch'}}
        plan,files = project_source_mapping(item.source,item.patch,**pins,limits=limits.planning)
        if any(name.lower() in {existing.lower() for existing in contents} for name in files):
            raise InstallError("Isolated projects have colliding target files")
        plans.append(plan)
        contents.update(files)
    from chatcopilot.core.archive_inventory import ZipInventoryLimits, _claim
    tree: dict[str, tuple[str, bool, bool]] = {}
    for name in contents:
        _claim(tree,name,False,ZipInventoryLimits(max_tree_entries=10000))
    combined = _CombinedPlan(tuple(file for plan in plans for file in plan.files),
        tuple(sorted(row[0] for row in tree.values() if row[1])))
    _budget(combined,workspace,limits.storage)
    return combined,contents,canonical_digest([asdict(plan) for plan in plans])


def _evidence(tree,plan,pin):
    return IsolatedProjectSources(str(tree.stage_path),pin,len(plan.files),len(plan.directories),sum(file.size for file in plan.files))


def materialize_project_sources_isolated(inputs: tuple[ProjectSourceInput,...], *, workspace: Path,
        limits: ProjectCopyLimits = ProjectCopyLimits()) -> IsolatedProjectSources:
    """New private empty lab only; plans are always rederived from original bytes."""
    workspace = Path(workspace)
    plan,contents,pin = _plan(inputs,workspace,limits)
    if shutil.disk_usage(workspace).free < sum(file.size for file in plan.files)+limits.min_free_bytes:
        raise InstallError("Project isolated copy requires configured free-space headroom")
    tree = None
    try:
        tree = IsolatedTree(workspace,create=True)
        for directory in plan.directories:
            tree.entry(directory,directory=True,create=True)
        tree.layout(plan.directories,())
        for file in plan.files:
            ref = tree.entry(file.path,directory=False,create=True)
            _write(tree,[ref],io.BytesIO(contents[file.path]),file.size,file.sha256)
        _verify(tree,plan)
        return _evidence(tree,plan,pin)
    except Exception as error:
        raise IsolatedProjectError(tree,error) from error
    finally:
        if tree is not None:
            tree.close()


def verify_project_sources_isolated(inputs: tuple[ProjectSourceInput,...], *, stage_root: Path,
        limits: ProjectCopyLimits = ProjectCopyLimits()) -> IsolatedProjectSources:
    """Read held files against rederived sources, never a saved staging receipt."""
    stage_root = Path(stage_root)
    plan,_contents,pin = _plan(inputs,stage_root,limits)
    tree = None
    try:
        tree = IsolatedTree(stage_root,create=False)
        for directory in plan.directories:
            tree.entry(directory,directory=True,create=False)
        for file in plan.files:
            tree.entry(file.path,directory=False,create=False)
        _verify(tree,plan)
        return _evidence(tree,plan,pin)
    except Exception as error:
        raise IsolatedProjectError(tree,error) from error
    finally:
        if tree is not None:
            tree.close()
