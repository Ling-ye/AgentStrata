"""Plan a pinned MCP source copy and original wheels; never run a build backend."""
from __future__ import annotations

import csv
from dataclasses import dataclass
import hashlib
import io
import json
import posixpath
import re
import tomllib

from packaging.requirements import Requirement

from chatcopilot.core.archive_inventory import _claim, _path
from chatcopilot.core.dependency_closure import windows_mcp_python_tags
from chatcopilot.core.install_artifacts import InstallError
from chatcopilot.core.mcp_inputs import locked_dependency_inputs
from chatcopilot.core.tar_inventory import TarInventoryLimits, read_git_source_members
from chatcopilot.core.wheel_installation import (
    EntryPointProvider, MappedEntryPoint, WindowsMappedWheelPlan, _wrapper,
    windows_mcp_wheel_installation_plan,
)
from chatcopilot.core.wheel_plan import PinnedWheel, PlannedWheelFile, WheelPlanLimits, _hash_field

RECIPE = "windows-mcp-source-copy/v1"
_SITE = "Lib/site-packages/"


@dataclass(frozen=True)
class MCPProjectLimits:
    archive: TarInventoryLimits = TarInventoryLimits(max_archive_bytes=128*1024*1024,
        max_stream_bytes=512*1024*1024, max_total_bytes=256*1024*1024, max_member_bytes=128*1024*1024,
        max_members=20000, max_tree_entries=40000)
    wheels: WheelPlanLimits = WheelPlanLimits()
    max_source_files: int = 512
    max_files: int = 20000
    max_directories: int = 5000
    max_total_bytes: int = 512*1024*1024

    def __post_init__(self):
        if (type(self.archive) is not TarInventoryLimits or type(self.wheels) is not WheelPlanLimits
                or any(type(value) is not int or value <= 0 for value in
                    (self.max_source_files, self.max_files, self.max_directories, self.max_total_bytes))):
            raise InstallError("MCP project planning requires explicit finite typed budgets")


@dataclass(frozen=True)
class WindowsMCPProjectPlan:
    source_archive_sha256: str
    revision: str
    project_sha256: str
    lock_sha256: str
    python_version: str
    wheels: WindowsMappedWheelPlan
    files: tuple[PlannedWheelFile, ...]
    directories: tuple[str, ...]
    source_files: tuple[str, ...]
    entry_point: MappedEntryPoint
    recipe: str = RECIPE
    schema: str = "agentstrata-windows-mcp-project-plan/v1"
    build_backend_executed: bool = False
    site_import: bool = False
    runtime_prefix_verified: bool = False
    native_abi_verified: bool = False
    installation_accepted: bool = False


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _requirement(text):
    if (type(text) is not str or not text.isascii() or not 0 < len(text) <= 4096
            or any(char in text for char in "\r\n\x00") or text.count("(") > 16):
        raise InstallError("MCP source requirement exceeds its syntax budget")
    value = Requirement(text)
    if value.url is not None:
        raise InstallError("MCP source-copy recipe requires named registry requirements")
    return value


def _metadata(project):
    lines = ["Metadata-Version: 2.4", "Name: windows-mcp", "Version: "+project["version"],
        "Requires-Python: "+project["requires-python"], "License-File: LICENSE.md"]
    for text in project["dependencies"]:
        lines.append("Requires-Dist: "+str(_requirement(text)))
    optional = project.get("optional-dependencies", {})
    if type(optional) is not dict or len(optional) > 32:
        raise InstallError("MCP optional metadata exceeds its budget")
    for extra, requirements in sorted(optional.items()):
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", extra) or type(requirements) is not list or len(requirements) > 128:
            raise InstallError("MCP optional metadata is unsupported")
        lines.append("Provides-Extra: "+extra)
        for text in requirements:
            value = _requirement(text)
            marker = ("("+str(value.marker)+") and " if value.marker else "")+"extra == '"+extra+"'"
            value.marker = None
            lines.append("Requires-Dist: "+str(value)+"; "+marker)
    return ("\n".join(lines)+"\n\n").encode("ascii")


def windows_mcp_project_plan(source: bytes, wheel_inputs: tuple[PinnedWheel, ...], *,
        expected_source_sha256: str, expected_revision: str, expected_project_sha256: str,
        expected_lock_sha256: str, python_version: str, recipe: str,
        limits: MCPProjectLimits = MCPProjectLimits()) -> WindowsMCPProjectPlan:
    """Reauthenticate original TAR and its locked wheel set before planning any destination.

    This explicit source recipe installs static Python package files and its own
    minimal distribution metadata. It does not reproduce an upstream wheel or
    consume a local build receipt. No project, startup or entry code executes.
    """
    if recipe != RECIPE or type(limits) is not MCPProjectLimits:
        raise InstallError("Unsupported MCP source installation recipe")
    if (type(expected_revision) is not str or re.fullmatch(r"[a-f0-9]{40}",expected_revision) is None
            or any(type(pin) is not str or re.fullmatch(r"[a-f0-9]{64}",pin) is None
                   for pin in (expected_source_sha256,expected_project_sha256,expected_lock_sha256))):
        raise InstallError("MCP source layout requires exact independent archive and metadata pins")
    windows_mcp_python_tags(python_version)
    if (type(wheel_inputs) is not tuple or not wheel_inputs
            or any(type(pin) is not PinnedWheel for pin in wheel_inputs)):
        raise InstallError("MCP source layout requires immutable original wheel inputs")
    prefix = "Windows-MCP-"+expected_revision+"/"
    names = ("pyproject.toml", "uv.lock", "LICENSE.md", "README.md")
    inventory, selected = read_git_source_members(source, expected_sha256=expected_source_sha256,
        expected_revision=expected_revision, members=tuple(prefix+name for name in names), limits=limits.archive)
    if (any(not file.path.startswith(prefix) for file in inventory.files)
            or any(path != prefix.rstrip("/") and not path.startswith(prefix) for path in inventory.directories)
            or _sha(selected[prefix+"pyproject.toml"]) != expected_project_sha256
            or _sha(selected[prefix+"uv.lock"]) != expected_lock_sha256):
        raise InstallError("MCP source layout differs from its independent archive and metadata pins")
    try:
        document = tomllib.loads(selected[prefix+"pyproject.toml"].decode("utf-8"))
        project = document["project"]
        lock = tomllib.loads(selected[prefix+"uv.lock"].decode("utf-8"))
        graph = locked_dependency_inputs(document, lock, python_version)
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        raise InstallError("MCP source metadata cannot supply this static installation recipe") from None
    if (project.get("name") != "windows-mcp" or type(project.get("version")) is not str
            or not re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,3}", project["version"])
            or project.get("dynamic") or project.get("scripts") != {"windows-mcp":"windows_mcp.__main__:main"}
            or project.get("gui-scripts") or project.get("entry-points")
            or project.get("readme") != "README.md" or project.get("license") != {"file":"LICENSE.md"}
            or document.get("build-system") != {"requires":["setuptools>=61"],"build-backend":"setuptools.build_meta"}
            or document.get("tool",{}).get("setuptools") != {"packages":{"find":{"where":["src"]}}}):
        raise InstallError("MCP project requires an unadmitted source or build transformation")
    selected_wheels = {(row["name"],row["version"],row["sha256"],row["bytes"]) for row in graph["selected"]}
    if (graph["blockers"] or len(wheel_inputs) != len(selected_wheels)
            or {(pin.name,pin.version,pin.sha256,len(pin.data)) for pin in wheel_inputs} != selected_wheels):
        raise InstallError("MCP installation wheels differ from its complete original source-lock selection")
    wheels = windows_mcp_wheel_installation_plan(tuple(sorted(wheel_inputs,key=lambda pin:pin.name)),
        python_version=python_version, requirements=tuple(project["dependencies"]), limits=limits.wheels)
    src = prefix+"src/windows_mcp/"
    source_files = [file for file in inventory.files if file.path.startswith(src)]
    source_paths = {file.path for file in source_files}
    if (not 2 <= len(source_files) <= limits.max_source_files
            or not {src+"__init__.py",src+"__main__.py"}.issubset(source_paths)
            or any(not file.path.endswith(".py") for file in source_files)
            or any(file.path.startswith(prefix+"src/") and not file.path.startswith(src) for file in inventory.files)
            or any(prefix+name in {file.path for file in inventory.files} for name in ("setup.py","setup.cfg","MANIFEST.in"))):
        raise InstallError("MCP source package files require another reviewed copy recipe")
    dist_info = "windows_mcp-"+project["version"]+".dist-info"
    protected = ("lib/site-packages/windows_mcp/", "lib/site-packages/"+dist_info.lower()+"/")
    if any(file.path.lower().startswith(protected) or file.path.lower() == "scripts/windows-mcp-script.py"
           or file.path.lower().startswith("lib/site-packages/windows_mcp.")
           for file in wheels.files):
        raise InstallError("MCP project collides with a wheel package, metadata or command namespace")
    output = list(wheels.files)
    owned = []
    def append(file):
        output.append(file)
        owned.append(file)
    for file in source_files:
        append(PlannedWheelFile(_SITE+file.path.removeprefix(prefix+"src/"), file.size, file.sha256,
            expected_source_sha256, file.path, recipe=RECIPE))
    by_name = {file.path:file for file in inventory.files}
    for name in ("LICENSE.md","README.md"):
        file = by_name[prefix+name]
        append(PlannedWheelFile(_SITE+dist_info+"/"+name,file.size,file.sha256,expected_source_sha256,file.path,recipe=RECIPE))
    provenance = json.dumps({"recipe":RECIPE,"source_archive_sha256":expected_source_sha256,"revision":expected_revision,
        "pyproject_sha256":expected_project_sha256,"uv_lock_sha256":expected_lock_sha256,
        "upstream_build_backend_executed":False},sort_keys=True,separators=(",",":")).encode()+b"\n"
    generated = {"METADATA":_metadata(project),"INSTALLER":b"agentstrata-source-copy\n",
        "entry_points.txt":b"[console_scripts]\nwindows-mcp = windows_mcp.__main__:main\n",
        "agentstrata-source.json":provenance}
    for name, raw in sorted(generated.items()):
        append(PlannedWheelFile(_SITE+dist_info+"/"+name,len(raw),_sha(raw),expected_source_sha256,None,raw,RECIPE))
    script = "Scripts/windows-mcp-script.py"
    raw = _wrapper("windows-mcp","windows_mcp.__main__",("main",))
    append(PlannedWheelFile(script,len(raw),_sha(raw),expected_source_sha256,None,raw,"guarded-python-entrypoint/v1"))
    stream = io.StringIO(newline="")
    rows = [(posixpath.relpath(file.path,_SITE.rstrip("/")),_hash_field(file.sha256),str(file.size)) for file in owned]
    rows.append((dist_info+"/RECORD","",""))
    csv.writer(stream,lineterminator="\n").writerows(sorted(rows))
    raw = stream.getvalue().encode("ascii")
    append(PlannedWheelFile(_SITE+dist_info+"/RECORD",len(raw),_sha(raw),expected_source_sha256,None,raw,"source-record/v1"))
    tree: dict[str, tuple[str,bool,bool]] = {}
    for path in wheels.directories:
        _claim(tree,path,True,limits.wheels.archive)
    for planned_file in output:
        _path(planned_file.path,limits.wheels.archive)
        _claim(tree,planned_file.path,False,limits.wheels.archive)
    directories = tuple(sorted(value[0] for value in tree.values() if value[1]))
    if (len(output) > limits.max_files or len(directories) > limits.max_directories
            or sum(file.size for file in output) > limits.max_total_bytes
            or sum(len(file.generated) for file in output if file.generated is not None) > limits.wheels.max_generated_bytes):
        raise InstallError("MCP merged source/wheel layout exceeds aggregate budgets")
    entry = MappedEntryPoint("windows-mcp","console_scripts","windows_mcp.__main__",("main",),(),
        (EntryPointProvider("windows-mcp",expected_source_sha256,expected_project_sha256),),script,
        "python.exe",("-I","-S",script),None)
    return WindowsMCPProjectPlan(expected_source_sha256,expected_revision,expected_project_sha256,expected_lock_sha256,
        python_version,wheels,tuple(sorted(output,key=lambda file:file.path)),directories,tuple(sorted(source_paths)),entry)
