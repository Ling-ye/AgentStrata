"""Deterministic source-to-prefix wheel layout; never execute startup or entry hooks."""
from __future__ import annotations

import configparser
import csv
from dataclasses import dataclass, replace
import hashlib
import io
import posixpath
import re
import zipfile

from chatcopilot.core.archive_inventory import _claim, _path
from chatcopilot.core.dependency_closure import (
    DependencyClosureLimits, WindowsDependencyClosure, windows_dependency_input_inventory, windows_python_tags,
    windows_mcp_dependency_input_inventory, windows_mcp_python_tags, WindowsDependencyInputInventory,
)
from chatcopilot.core.install_artifacts import InstallError
from chatcopilot.core.wheel_plan import (
    PinnedWheel, PlannedWheel, PlannedWheelFile, WheelPlanLimits, WindowsWheelPlan,
    _hash_field, _headers, _map, _name, _one,
)

_SITE = "Lib/site-packages/"
_IDENTIFIER = r"[A-Za-z_][A-Za-z0-9_]*"
_OBJECT = re.compile(rf"({_IDENTIFIER}(?:\.{_IDENTIFIER})*)\s*:\s*({_IDENTIFIER}(?:\.{_IDENTIFIER})*)(?:\s*\[([^\[\]]+)\])?")


@dataclass(frozen=True)
class EntryPointProvider:
    name: str
    archive_sha256: str
    metadata_sha256: str


@dataclass(frozen=True)
class MappedEntryPoint:
    name: str
    group: str
    module: str
    attributes: tuple[str, ...]
    extras: tuple[str, ...]
    providers: tuple[EntryPointProvider, ...]
    script_path: str | None
    interpreter: str
    arguments: tuple[str, ...]
    deferred_reason: str | None
    requires_runtime_acceptance: bool = True


@dataclass(frozen=True)
class DeferredWheelFile:
    distribution: str
    source_path: str
    target_path: str
    sha256: str
    reason: str


_Entry = tuple[str, str, str, tuple[str, ...], tuple[str, ...], EntryPointProvider, str | None]


class _EntryParser(configparser.ConfigParser):
    def optionxform(self, optionstr: str) -> str:
        return optionstr


@dataclass(frozen=True, kw_only=True)
class WindowsMappedWheelPlan(WindowsWheelPlan):
    dependency_closure: WindowsDependencyClosure
    entry_points: tuple[MappedEntryPoint, ...]
    deferred_files: tuple[DeferredWheelFile, ...]
    recipe: str = "windows-wheel-layout/v2"
    site_import: bool = False
    runtime_prefix_verified: bool = False
    native_abi_verified: bool = False
    schema: str = "agentstrata-windows-wheel-plan/v2"


def _startup(path):
    return any(part == "pyvenv.cfg" or part.endswith((".pth", "._pth"))
               or part in {"sitecustomize", "usercustomize"}
               or part.startswith(("sitecustomize.", "usercustomize.")) for part in path.lower().split("/"))


def _deferred(name, path, reason):
    digest = hashlib.sha256(path.encode("ascii")).hexdigest()
    return "Disabled/" + reason + "/" + name + "/" + digest + ".bin"


def _target(path, directory, wheel):
    data = wheel.dist_info.removesuffix(".dist-info") + ".data"
    parts = path.split("/")
    if parts[0] == data and len(parts) >= 2 and parts[1] == "scripts":
        if len(parts) == 2 and not directory:
            raise InstallError("Wheel scripts scheme must be a directory")
        return None if directory else (_deferred(wheel.name, path, "scripts"), "deferred-script")
    mapped = _map(path, directory, wheel.dist_info)
    if _startup(path):
        return None if directory else (_deferred(wheel.name, path, "startup"), "disabled-startup")
    return (mapped, None) if mapped is not None else None


def _wrapper(name, module, attributes):
    # Validate metadata before interpolation; literals never become Python syntax.
    source = '''"""Generated guarded entry point; independent runtime acceptance is still required."""
import os
import sys

def _main():
    if not sys.flags.isolated or not sys.flags.no_site:
        raise RuntimeError("Entry point requires isolated Python with site disabled")
    _prefix = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if os.path.normcase(os.path.abspath(sys.prefix)) != os.path.normcase(_prefix):
        raise RuntimeError("Entry point requires the independently configured environment prefix")
    from importlib import import_module
    _entry = import_module({module!r})
    for _attribute in {attributes!r}:
        _entry = getattr(_entry, _attribute)
    sys.argv[0] = {name!r}
    return _entry()

if __name__ == "__main__":
    sys.exit(_main())
'''
    return source.format(module=module, attributes=attributes, name=name).encode("ascii")


def _entries(data: bytes, provider: EntryPointProvider, active_extras: set[str]) -> list[_Entry]:
    if len(data) > 128 * 1024:
        raise InstallError("Wheel entry-point metadata exceeds its byte budget")
    parser = _EntryParser(interpolation=None, delimiters=("=",), strict=True, empty_lines_in_values=False)
    try:
        parser.read_string(data.decode("utf-8"))
    except (configparser.Error, UnicodeError):
        raise InstallError("Wheel entry-point metadata is invalid") from None
    if parser.defaults() or len(parser.sections()) > 64:
        raise InstallError("Wheel entry-point defaults or group budget are unsupported")
    output: list[_Entry] = []
    count = 0
    for group in parser.sections():
        if not re.fullmatch(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*", group):
            raise InstallError("Wheel entry-point group is invalid")
        items = list(parser.items(group))
        count += len(items)
        if count > 512:
            raise InstallError("Wheel entry-point count exceeds its budget")
        if group not in {"console_scripts", "gui_scripts"}:
            continue  # Preserve plugin metadata as bytes; never load a plugin here.
        for name, value in items:
            if (len(name) > 80 or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", name)
                    or name.lower() in {"python", "pythonw", "activate", "deactivate"}):
                raise InstallError("Wheel entry-point command name is unsafe")
            _path(name + ".exe", WheelPlanLimits().archive)
            match = _OBJECT.fullmatch(value.strip()) if len(value) <= 4096 else None
            if match is None:
                raise InstallError("Wheel script entry requires a bounded module and callable reference")
            module, attribute, extras_text = match.groups()
            extras = tuple(sorted({_name(item.strip()) for item in extras_text.split(",")})) if extras_text else ()
            if len(extras) > 32:
                raise InstallError("Wheel entry-point extras exceed their budget")
            reason = None if set(extras).issubset(active_extras) else "unactivated-extra"
            output.append((name, group, module, tuple(attribute.split(".")), extras, provider, reason))
    return output


def windows_wheel_installation_plan(inputs: tuple[PinnedWheel, ...], *, python_version: str,
        requirements: tuple[str, ...], limits: WheelPlanLimits = WheelPlanLimits(),
        dependency_limits: DependencyClosureLimits = DependencyClosureLimits()) -> WindowsMappedWheelPlan:
    """Map every source member, quarantine startup/data scripts and generate guarded wrappers.

    This recipe creates Python entry scripts and explicit interpreter arguments,
    not binary launchers or a runtime acceptance. Legacy byte-plan APIs are not
    changed. All package startup and postinstall code remains unexecuted.
    """
    checked = windows_dependency_input_inventory(inputs, python_version=python_version, requirements=requirements,
        limits=dependency_limits, wheel_limits=limits)
    return _map_checked(inputs, checked, windows_python_tags(python_version), limits)


def windows_mcp_wheel_installation_plan(inputs: tuple[PinnedWheel, ...], *, python_version: str,
        requirements: tuple[str, ...], limits: WheelPlanLimits = WheelPlanLimits(),
        dependency_limits: DependencyClosureLimits = DependencyClosureLimits()) -> WindowsMappedWheelPlan:
    """Plan MCP-only original wheel data with its explicit legacy header policy.

    The generic installation API and every environment/ABI admission policy keep
    their existing scope. No ZIP is normalized or rewritten, and no hook runs.
    """
    checked = windows_mcp_dependency_input_inventory(inputs, python_version=python_version, requirements=requirements,
        limits=dependency_limits, wheel_limits=limits)
    result = _map_checked(inputs, checked, windows_mcp_python_tags(python_version), limits, legacy_root=True)
    return replace(result, recipe="windows-mcp-wheel-layout/v1", schema="agentstrata-windows-mcp-wheel-plan/v1")


def _map_checked(inputs: tuple[PinnedWheel, ...], checked: WindowsDependencyInputInventory,
                 tags: tuple[str, ...], limits: WheelPlanLimits, *, legacy_root: bool = False) -> WindowsMappedWheelPlan:
    by_hash = {pin.sha256: pin.data for pin in inputs}
    activated = {node.name: set(node.activated_extras) for node in checked.closure.nodes}
    tree: dict[str, tuple[str, bool, bool]] = {}
    directories: set[str] = set()
    output: list[PlannedWheelFile] = []
    deferred: list[DeferredWheelFile] = []
    scripts: dict[str, list[_Entry]] = {}
    planned: list[PlannedWheel] = []
    by_distribution: dict[str, list[PlannedWheelFile]] = {}
    total = generated = 0

    def claim(path, directory):
        _path(path + ("/" if directory else ""), limits.archive)
        if not directory or path not in directories:
            _claim(tree, path, directory, limits.archive)
        if directory:
            directories.add(path)

    def append(file, owners):
        nonlocal total, generated
        claim(file.path, False)
        total += file.size
        generated += len(file.generated) if file.generated is not None else 0
        if total > limits.archive.max_total_bytes or generated > limits.max_generated_bytes:
            raise InstallError("Wheel mapped layout exceeds aggregate byte budgets")
        output.append(file)
        for owner in owners:
            by_distribution[owner].append(file)

    for wheel in checked.inventory.wheels:
        by_distribution[wheel.name] = []
        files = {file.path: file for file in wheel.inventory.files}
        record_path = wheel.dist_info + "/RECORD"
        if any(path.startswith(wheel.dist_info + "/") and path.split("/")[1].lower() in
               {"installer", "requested", "direct_url.json", "record.jws", "record.p7s"}
               for path in (*wheel.inventory.directories, *files)):
            raise InstallError("Wheel installer-supplied metadata is unsupported")
        for path in wheel.inventory.directories:
            target = _target(path, True, wheel)
            if target is not None:
                claim(target[0], True)
        for file in wheel.inventory.files:
            if file.path == record_path:
                continue
            target = _target(file.path, False, wheel)
            if target is None:
                raise InstallError("Wheel source member lacks a defined mapped destination")
            path, reason = target
            append(PlannedWheelFile(path, file.size, file.sha256, wheel.archive_sha256, file.path,
                recipe="wheel-relocate/v2" if reason is None else reason + "/v1"), (wheel.name,))
            if reason:
                deferred.append(DeferredWheelFile(wheel.name, file.path, path, file.sha256, reason))
        entry_path = wheel.dist_info + "/entry_points.txt"
        if entry_path in files:
            if files[entry_path].size > 128 * 1024:
                raise InstallError("Wheel entry-point metadata exceeds its byte budget")
            provider = EntryPointProvider(wheel.name, wheel.archive_sha256, files[entry_path].sha256)
            with zipfile.ZipFile(io.BytesIO(by_hash[wheel.archive_sha256])) as archive:
                for entry in _entries(archive.read(entry_path), provider, activated[wheel.name]):
                    name = entry[0].casefold()
                    if name in scripts and scripts[name][0][:5] != entry[:5]:
                        raise InstallError("Wheel script entry points collide")
                    scripts.setdefault(name, []).append(entry)
        installer = b"agentstrata\n"
        append(PlannedWheelFile(_SITE + wheel.dist_info + "/INSTALLER", len(installer), hashlib.sha256(installer).hexdigest(),
            wheel.archive_sha256, None, installer, "installer/v2"), (wheel.name,))
        with zipfile.ZipFile(io.BytesIO(by_hash[wheel.archive_sha256])) as archive:
            root_value = _one(_headers(archive.read(wheel.dist_info + "/WHEEL")), "Root-Is-Purelib")
            purelib = (root_value.lower() if legacy_root else root_value) == "true"
        planned.append(PlannedWheel(wheel.name, wheel.version, wheel.archive_sha256, wheel.dist_info,
            purelib, wheel.tags, files[record_path]))

    entries = []
    for _, providers in sorted(scripts.items()):
        name, group, module, attributes, extras, _, _ = providers[0]
        owners = tuple(sorted((entry[5] for entry in providers), key=lambda entry: entry.name))
        if any(entry[6] for entry in providers):
            entries.append(MappedEntryPoint(name, group, module, attributes, extras, owners, None,
                "pythonw.exe" if group == "gui_scripts" else "python.exe", (), "unactivated-extra"))
            continue
        path = "Scripts/" + name + "-script.py"
        raw = _wrapper(name, module, attributes)
        source_pin = hashlib.sha256("\n".join(owner.archive_sha256 for owner in owners).encode()).hexdigest()
        append(PlannedWheelFile(path, len(raw), hashlib.sha256(raw).hexdigest(), source_pin, None, raw,
            "guarded-python-entrypoint/v1"), tuple(owner.name for owner in owners))
        entries.append(MappedEntryPoint(name, group, module, attributes, extras, owners, path,
            "pythonw.exe" if group == "gui_scripts" else "python.exe", ("-I", "-S", path), None))

    for mapped_wheel in planned:
        path = _SITE + mapped_wheel.dist_info + "/RECORD"
        rows = [(posixpath.relpath(file.path, _SITE.rstrip("/")), _hash_field(file.sha256), str(file.size))
                for file in by_distribution[mapped_wheel.name]]
        rows.append((mapped_wheel.dist_info + "/RECORD", "", ""))
        stream = io.StringIO(newline="")
        csv.writer(stream, lineterminator="\n").writerows(sorted(rows))
        raw = stream.getvalue().encode("ascii")
        append(PlannedWheelFile(path, len(raw), hashlib.sha256(raw).hexdigest(), mapped_wheel.archive_sha256,
            None, raw, "mapped-record/v2"), ())
    return WindowsMappedWheelPlan(tuple(planned), tuple(sorted(tags)),
        tuple(sorted(output, key=lambda file: file.path)), tuple(sorted(value[0] for value in tree.values() if value[1])),
        dependency_closure=checked.closure, entry_points=tuple(entries), deferred_files=tuple(sorted(deferred, key=lambda item:item.target_path)))
