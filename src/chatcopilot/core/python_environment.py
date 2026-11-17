"""Plan a pinned Windows venv image; plans and bindings never authorize launch."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import posixpath
import re

from chatcopilot.core.archive_inventory import ArchiveFile, ZipInventoryLimits, _claim, _path
from chatcopilot.core.dependency_closure import (
    DependencyClosureLimits, WindowsDependencyClosure, windows_dependency_plan,
)
from chatcopilot.core.install_artifacts import InstallError
from chatcopilot.core.wheel_installation import windows_wheel_installation_plan
from chatcopilot.core.python_materialize import PythonMaterializeLimits
from chatcopilot.core.python_plan import (
    WindowsPythonPlan, _bytes, _digest, _json, _keys, windows_python_plan,
)
from chatcopilot.core.wheel_plan import (
    PinnedWheel, WheelPlanLimits, WindowsWheelPlan, _name, windows_wheel_plan,
)

_RECIPE = "windows-isolated-venv/v1"
_MAPPED_RECIPE = "windows-isolated-venv/v2"
_SITE = "Lib/site-packages"
_CONFIG_LIMIT = 4095  # Below the smaller pinned 3.12 redirector's 4096-byte buffer.


def canonical_digest(value) -> str:
    def encode(item):
        if type(item) is bytes:
            return {"bytes_hex": item.hex()}
        raise TypeError("Unsupported environment fact type")
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     default=encode).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PythonEnvironmentLimits:
    python: PythonMaterializeLimits = PythonMaterializeLimits()
    wheels: WheelPlanLimits = WheelPlanLimits()
    max_inputs_bytes: int = 64 * 1024
    dependencies: DependencyClosureLimits = DependencyClosureLimits()

    def __post_init__(self):
        if (type(self.python) is not PythonMaterializeLimits or type(self.wheels) is not WheelPlanLimits
                or type(self.max_inputs_bytes) is not int or self.max_inputs_bytes <= 0
                or type(self.dependencies) is not DependencyClosureLimits):
            raise InstallError("Environment limits require bounded Python, wheel and immutable input limits")


@dataclass(frozen=True)
class EnvironmentFile:
    path: str
    size: int
    sha256: str
    input_sha256: str
    source_kind: str
    source_path: str | None
    generated: bytes | None = None
    recipe: str | None = None


@dataclass(frozen=True)
class WindowsPythonEnvironmentPlan:
    inputs_sha256: str
    python: WindowsPythonPlan
    wheels: WindowsWheelPlan
    environment_root: str
    files: tuple[EnvironmentFile, ...]
    directories: tuple[str, ...]
    configuration_paths: tuple[str, ...]
    module_paths: tuple[str, ...]
    site_import: bool = False
    recipe: str = _RECIPE
    schema: str = "agentstrata-windows-python-environment-plan/v1"


@dataclass(frozen=True, kw_only=True)
class WindowsPythonClosedEnvironmentPlan(WindowsPythonEnvironmentPlan):
    dependency_closure: WindowsDependencyClosure
    schema: str = "agentstrata-windows-python-environment-plan/v2"


@dataclass(frozen=True, kw_only=True)
class WindowsPythonMappedEnvironmentPlan(WindowsPythonClosedEnvironmentPlan):
    recipe: str = _MAPPED_RECIPE
    schema: str = "agentstrata-windows-python-environment-plan/v3"


@dataclass(frozen=True)
class BoundPythonEnvironment:
    plan_sha256: str
    physical_root: str
    files: tuple[EnvironmentFile, ...]
    directories: tuple[str, ...]
    schema: str = "agentstrata-bound-python-environment/v1"


def _wheel_inputs(raw, archives, limits):
    if type(raw) is not list or not 0 < len(raw) <= limits.wheels.max_wheels:
        raise InstallError("Environment requires a bounded frozen wheel set")
    if (type(archives) is not tuple or len(archives) != len(raw)
            or any(type(data) is not bytes for data in archives)
            or sum(len(data) for data in archives) > limits.wheels.archive.max_archive_bytes):
        raise InstallError("Environment wheel archives differ from the immutable input set or byte budget")
    by_hash = {hashlib.sha256(data).hexdigest(): data for data in archives}
    if len(by_hash) != len(archives):
        raise InstallError("Environment wheel archive repeats")
    output, names, pins = [], set(), set()
    for value in raw:
        item = _keys(value, {"name", "version", "sha256"})
        name, pin = _name(item["name"]), _digest(item["sha256"])
        if name != item["name"] or name in names or pin in pins or pin not in by_hash:
            raise InstallError("Environment wheel identities or independent pins differ")
        names.add(name)
        pins.add(pin)
        output.append(PinnedWheel(by_hash[pin], pin, name, item["version"]))
    if pins != by_hash.keys():
        raise InstallError("Environment has unadmitted wheel bytes")
    return tuple(output)


def _loading_policy(python: WindowsPythonPlan, wheels: WindowsWheelPlan):
    protected = {"sys", "builtins", "nt", "_frozen_importlib", "_frozen_importlib_external",
                 "site", "os", "encodings", "importlib"}
    for file in python.files:
        name = file.source_path.removeprefix(python.pin.root + "/")
        if name.startswith(("Lib/", "DLLs/")):
            part = name.split("/")[1].lower()
            if part not in {"site-packages", "__pycache__"}:
                protected.add(part.split(".")[0])
    for path in (*wheels.directories, *(file.path for file in wheels.files)):
        parts = path.lower().split("/")
        if any(part == "pyvenv.cfg" or part.endswith((".pth", "._pth"))
               or part in {"sitecustomize", "usercustomize"}
               or part.startswith(("sitecustomize.", "usercustomize.")) for part in parts):
            raise InstallError("Wheel requires an undefined startup or module-path transformation")
        if path.startswith(_SITE + "/"):
            top = parts[2]
            if not top.endswith((".dist-info", ".egg-info")) and top.split(".")[0] in protected:
                raise InstallError("Wheel would shadow a pinned standard-library or bootstrap module")


def windows_python_environment_plan(inputs: bytes, python_inputs: bytes, metadata: bytes,
                                    python_archive: bytes, wheel_archives: tuple[bytes, ...], *,
                                    expected_inputs_sha256: str,
                                    limits: PythonEnvironmentLimits = PythonEnvironmentLimits()) -> WindowsPythonEnvironmentPlan:
    """Authenticate an independent environment root before parsing or filesystem use.

    The root binds the Python input-lock digest, runtime lane, complete wheel set
    and recipe. Compatibility tags derive from that pinned Windows x64 CPython;
    current interpreters, environment snapshots and returned plans are not inputs.
    """
    _bytes(inputs, limits.max_inputs_bytes)
    digest = hashlib.sha256(inputs).hexdigest()
    if digest != _digest(expected_inputs_sha256):
        raise InstallError("Environment inputs differ from the independent host pin")
    document = _json(inputs, limits.python.planning)
    mapped = document.get("schema") == "agentstrata-windows-python-environment-inputs/v3"
    closed = document.get("schema") in ("agentstrata-windows-python-environment-inputs/v2", "agentstrata-windows-python-environment-inputs/v3")
    _keys(document, {"schema", "recipe", "python_inputs_sha256", "lane", "wheels"} | ({"requirements"} if closed else set()))
    if (type(document["schema"]) is not str
            or document["schema"] not in {"agentstrata-windows-python-environment-inputs/v1", "agentstrata-windows-python-environment-inputs/v2", "agentstrata-windows-python-environment-inputs/v3"}
            or document["recipe"] != (_MAPPED_RECIPE if mapped else _RECIPE)):
        raise InstallError("Unsupported environment input schema or recipe")
    if document["lane"] == "windows_mcp_python":
        raise InstallError("Windows-MCP venv, loading and ABI recipes are not admitted")
    python = windows_python_plan(python_inputs, metadata, python_archive,
        expected_inputs_sha256=_digest(document["python_inputs_sha256"]), lane=document["lane"], limits=limits.python.planning)
    major, minor, _ = python.pin.version.split(".")
    tags = (f"cp{major}{minor}-cp{major}{minor}-win_amd64", "py3-none-any")
    pinned = _wheel_inputs(document["wheels"], wheel_archives, limits)
    dependency_closure = None
    wheels: WindowsWheelPlan
    if mapped:
        if type(document["requirements"]) is not list:
            raise InstallError("Environment v3 requires an explicit root requirement list")
        mapped_wheels = windows_wheel_installation_plan(pinned, python_version=python.pin.version,
            requirements=tuple(document["requirements"]), limits=limits.wheels, dependency_limits=limits.dependencies)
        wheels = mapped_wheels
        dependency_closure = mapped_wheels.dependency_closure
    elif closed:
        if type(document["requirements"]) is not list:
            raise InstallError("Environment v2 requires an explicit root requirement list")
        checked = windows_dependency_plan(pinned, python_version=python.pin.version,
            requirements=tuple(document["requirements"]), limits=limits.dependencies, wheel_limits=limits.wheels)
        wheels, dependency_closure = checked.wheels, checked.closure
    else:
        wheels = windows_wheel_plan(pinned, compatible_tags=tags, limits=limits.wheels)
    _loading_policy(python, wheels)
    members = {file.source_path: file for file in python.files}
    launchers = ("python.exe", "pythonw.exe") if minor == "12" else ("venvlauncher.exe", "venvwlauncher.exe")
    required = (*("Lib/venv/scripts/nt/" + name for name in launchers), "DLLs/_socket.pyd")
    if any(python.pin.root + "/" + name not in members or not members[python.pin.root + "/" + name].size for name in required):
        raise InstallError("Pinned Python image lacks required Windows venv redirectors or extension layout")
    slot = "host" if python.pin.lane == "host_python" else "vtuber"
    environment_root = "runtime/venv/" + slot + "/" + digest
    files = [EnvironmentFile(file.path, file.size, file.sha256, python.pin.sha256, "python", file.source_path)
             for file in python.files]
    for name, source in zip(("python.exe", "pythonw.exe"), required[:2], strict=True):
        file = members[python.pin.root + "/" + source]
        files.append(EnvironmentFile(environment_root + "/Scripts/" + name, file.size, file.sha256,
                                     python.pin.sha256, "python", file.source_path, recipe="venv-redirector-copy/v1"))
    for wheel_file in wheels.files:
        files.append(EnvironmentFile(environment_root + "/" + wheel_file.path, wheel_file.size, wheel_file.sha256, wheel_file.input_sha256,
            "wheel" if wheel_file.source_path is not None else "generated", wheel_file.source_path, wheel_file.generated, wheel_file.recipe))
    configuration_paths = (environment_root + "/pyvenv.cfg", python.target_root + f"/python{major}{minor}._pth")
    paths = set(python.directories) | {environment_root + "/" + path for path in wheels.directories}
    paths |= {environment_root, environment_root + "/Scripts", environment_root + "/Include", environment_root + "/" + _SITE}
    tree: dict[str, tuple[str, bool, bool]] = {}
    path_limits = ZipInventoryLimits(max_path_bytes=limits.python.planning.archive.max_path_bytes,
        max_depth=limits.python.planning.archive.max_depth,
        max_tree_entries=limits.python.planning.archive.max_tree_entries)
    for path in sorted(paths):
        _path(path + "/", path_limits)
        _claim(tree, path, True, path_limits)
    for path in [file.path for file in files] + list(configuration_paths):
        _path(path, path_limits)
        _claim(tree, path, False, path_limits)
    directories = tuple(sorted(value[0] for value in tree.values() if value[1]))
    if (len(files) + 2 > limits.python.max_files or len(directories) > limits.python.max_directories
            or sum(file.size for file in files) + 2 * _CONFIG_LIMIT > limits.python.max_total_bytes):
        raise InstallError("Environment exceeds aggregate staging budgets")
    common = (digest, python, wheels, environment_root,
        tuple(sorted(files, key=lambda file: file.path)), directories, configuration_paths,
        (python.target_root + "/Lib", python.target_root + "/DLLs", environment_root + "/" + _SITE))
    if mapped:
        if dependency_closure is None:
            raise InstallError("Mapped environment lacks its dependency closure")
        return WindowsPythonMappedEnvironmentPlan(*common, dependency_closure=dependency_closure)
    if dependency_closure is not None:
        return WindowsPythonClosedEnvironmentPlan(*common, dependency_closure=dependency_closure)
    return WindowsPythonEnvironmentPlan(*common)


def _bind(plan: WindowsPythonEnvironmentPlan, physical_root: str) -> BoundPythonEnvironment:
    # Only the Windows materializer calls this after anchoring a private tree.
    # This pure helper neither validates storage nor grants provenance to a plan.
    if (type(physical_root) is not str or not re.fullmatch(r"\\\\\?\\Volume\{[0-9a-fA-F-]+\}(?:\\[^\\]+)+", physical_root)
            or any(ord(char) < 32 or ord(char) == 127 for char in physical_root)
            or any(part in {".", ".."} for part in physical_root.split("\\"))):
        raise InstallError("Environment binding requires an anchored Windows volume path")
    home = physical_root + "\\" + plan.python.target_root.replace("/", "\\")
    cfg = (f"home = {home}\ninclude-system-site-packages = false\nversion = {plan.python.pin.version}\n").encode("utf-8")
    relative = [posixpath.relpath(path, plan.python.target_root) for path in plan.module_paths]
    if any(posixpath.normpath(posixpath.join(plan.python.target_root, path)) != target
           or target.startswith("../") for path, target in zip(relative, plan.module_paths, strict=True)):
        raise InstallError("Generated module path escaped its independently planned tree")
    # Deliberately do not import site: automatic prefix/site customization is
    # outside this byte recipe. Explicit wheel search paths need no site hooks.
    pth = ("\n".join(path.replace("/", "\\") for path in relative) + "\n").encode("ascii")
    if max(len(cfg), len(pth)) > _CONFIG_LIMIT:
        raise InstallError("Generated venv configuration exceeds pinned redirector limits")
    output = list(plan.files)
    for path, data, recipe in zip(plan.configuration_paths, (cfg, pth), ("pyvenv-cfg/v1", "isolated-module-path/v1"), strict=True):
        output.append(EnvironmentFile(path, len(data), hashlib.sha256(data).hexdigest(), plan.inputs_sha256,
                                      "generated", None, data, recipe))
    return BoundPythonEnvironment(canonical_digest(asdict(plan)), physical_root,
                                   tuple(sorted(output, key=lambda file: file.path)), plan.directories)


def _budget_files(plan: WindowsPythonEnvironmentPlan) -> tuple[ArchiveFile, ...]:
    return tuple(ArchiveFile(file.path, file.size, file.sha256) for file in plan.files) + tuple(
        ArchiveFile(path, _CONFIG_LIMIT, "0" * 64) for path in plan.configuration_paths)
