"""Derive a Windows wheel byte plan from independently pinned immutable inputs.

This restricted policy writes nothing and runs no package code. A plan describes
bytes and their provenance; it is not an installed-state or acceptance receipt.
"""
from __future__ import annotations

import base64
import csv
from dataclasses import dataclass, replace
from email import policy
from email.parser import Parser
import hashlib
import io
import re
import zipfile
from urllib.parse import urlsplit

from packaging.utils import parse_wheel_filename
from packaging.version import Version

from chatcopilot.core.archive_inventory import ZipInventory, ArchiveFile, ZipInventoryLimits, _claim, _path, zip_inventory, plugin_zip_inventory
from chatcopilot.core.install_artifacts import InstallError

_SITE = "Lib/site-packages/"
_MIB = 1024 * 1024
_INSTALLER = b"agentstrata\n"
_METADATA_SUFFIXES = (".dist-info", ".egg-info", ".data")
_VERSION = (r"(?:[0-9]+!)?[0-9]+(?:\.[0-9]+)*(?:(?:a|b|rc)[0-9]+)?"
            r"(?:\.post[0-9]+)?(?:\.dev[0-9]+)?(?:\+[a-z0-9]+(?:\.[a-z0-9]+)*)?")


@dataclass(frozen=True)
class PinnedWheel:
    data: bytes
    sha256: str
    name: str
    version: str


@dataclass(frozen=True)
class WheelPlanLimits:
    archive: ZipInventoryLimits = ZipInventoryLimits()
    max_wheels: int = 256
    max_metadata_bytes: int = 4 * _MIB
    max_generated_bytes: int = 16 * _MIB

    def __post_init__(self):
        if type(self.archive) is not ZipInventoryLimits or any(
                type(value) is not int or value <= 0 for value in
                (self.max_wheels, self.max_metadata_bytes, self.max_generated_bytes)):
            raise InstallError("Wheel plan limits must be positive integers with ZIP limits")


@dataclass(frozen=True)
class PlannedWheelFile:
    path: str
    size: int
    sha256: str
    input_sha256: str
    source_path: str | None
    generated: bytes | None = None
    recipe: str | None = None


@dataclass(frozen=True)
class PlannedWheel:
    name: str
    version: str
    archive_sha256: str
    dist_info: str
    root_is_purelib: bool
    tags: tuple[str, ...]
    replaced_record: ArchiveFile


@dataclass(frozen=True)
class WindowsWheelPlan:
    wheels: tuple[PlannedWheel, ...]
    compatible_tags: tuple[str, ...]
    files: tuple[PlannedWheelFile, ...]
    directories: tuple[str, ...]
    schema: str = "agentstrata-windows-wheel-plan/v1"


def _name(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,198}[A-Za-z0-9])?", value):
        raise InstallError("Wheel distribution name is invalid")
    return re.sub(r"[-_.]+", "-", value).lower()


def _tags(values: tuple[str, ...], *, windows: bool = False) -> tuple[str, ...]:
    if (type(values) is not tuple or not values or len(values) > 256 or any(
            not isinstance(value, str) or len(value) > 128 or not re.fullmatch(r"[a-z0-9_]+-[a-z0-9_]+-[a-z0-9_]+", value)
            or (windows and value.rsplit("-", 1)[1] not in {"any", "win32", "win_amd64", "win_arm64"}) for value in values)):
        raise InstallError("Explicit expanded Windows compatibility tags are required")
    if len(set(values)) != len(values):
        raise InstallError("Wheel compatibility tags repeat")
    return tuple(sorted(values))


def _headers(data: bytes):
    # Bound email object allocation before parsing. Only headers use this line
    # budget; the bounded UTF-8 description body remains byte-preserved.
    header = data.replace(b"\r\n", b"\n").partition(b"\n\n")[0]
    if (b"\r" in header or header.count(b"\n") >= 1024
            or any(len(line) > 64 * 1024 for line in header.split(b"\n"))):
        raise InstallError("Wheel metadata header budget or line endings are unsupported")
    try:
        message = Parser(policy=policy.default).parsestr(data.decode("utf-8"))
    except (UnicodeError, ValueError):
        raise InstallError("Invalid UTF-8 wheel metadata") from None
    if message.defects or any(getattr(value, "defects", ()) for value in message.values()):
        raise InstallError("Malformed wheel metadata headers")
    return message


def _values(message, name: str) -> list[str]:
    # Compare literal identity fields, without email encoded-word decoding or
    # header unfolding that could hide a different on-disk interpretation.
    return [value for key, value in message.raw_items() if key.lower() == name.lower()]


def _one(message, name: str) -> str:
    values = _values(message, name)
    if len(values) != 1:
        raise InstallError("Missing or repeated wheel identity headers")
    return str(values[0])


def _hash_field(digest: str) -> str:
    return "sha256=" + base64.urlsafe_b64encode(bytes.fromhex(digest)).rstrip(b"=").decode("ascii")


def _record(data: bytes, files: dict[str, ArchiveFile], record_path: str) -> None:
    seen = set()
    try:
        for row in csv.reader(io.StringIO(data.decode("utf-8"), newline=""), strict=True):
            if len(row) != 3 or row[0] in seen or row[0] not in files:
                raise InstallError("Wheel RECORD paths differ from archive inventory")
            path, digest, size = row
            seen.add(path)
            item = files[path]
            if (digest, size) != (("", "") if path == record_path else (_hash_field(item.sha256), str(item.size))):
                raise InstallError("Wheel RECORD hash or size differs from archive inventory")
    except (UnicodeError, csv.Error):
        raise InstallError("Invalid UTF-8 wheel RECORD CSV") from None
    if seen != files.keys():
        raise InstallError("Wheel RECORD does not cover every archive file")


def _map(path: str, directory: bool, dist_info: str) -> str | None:
    parts = path.split("/")
    first = parts[0]
    data_root = dist_info.removesuffix(".dist-info") + ".data"
    if first.lower().endswith(_METADATA_SUFFIXES) and first not in {dist_info, data_root}:
        raise InstallError("Foreign or ambiguous wheel metadata root")
    if first == data_root:
        if len(parts) == 1:
            if not directory:
                raise InstallError("Wheel data root must be a directory")
            return None
        if parts[1] not in {"purelib", "platlib"}:
            raise InstallError("Wheel install scheme requires an unsupported transformation")
        if len(parts) == 2:
            if not directory:
                raise InstallError("Wheel install scheme must be a directory")
            return None
        parts = parts[2:]
        if parts[0].lower().endswith(_METADATA_SUFFIXES):
            raise InstallError("Wheel data cannot inject installation metadata")
    return _SITE + "/".join(parts)


def windows_wheel_plan(inputs: tuple[PinnedWheel, ...], *, compatible_tags: tuple[str, ...],
                       limits: WheelPlanLimits = WheelPlanLimits()) -> WindowsWheelPlan:
    """Plan a frozen wheel set for a future owned Windows venv, without installing.

    Names, exact versions, archive pins and expanded compatible tags must come
    from host-controlled frozen inputs, never from the environment being checked.
    ZIP limits bound each input. Archive-byte, output-byte and destination-tree
    budgets also bound the whole set.
    """
    tags = _tags(compatible_tags, windows=True)
    if type(inputs) is not tuple or not inputs or len(inputs) > limits.max_wheels:
        raise InstallError("A bounded immutable wheel input set is required")
    if any(type(item) is not PinnedWheel or type(item.data) is not bytes for item in inputs):
        raise InstallError("Wheel inputs require pinned immutable archive bytes")
    if sum(len(item.data) for item in inputs) > limits.archive.max_archive_bytes:
        raise InstallError("Wheel input set exceeds archive byte limit")
    identities = set()
    for item in inputs:
        name = _name(item.name)
        if name in identities:
            raise InstallError("Wheel distribution appears more than once")
        identities.add(name)
        if not isinstance(item.version, str) or len(item.version) > 128 or not re.fullmatch(_VERSION, item.version):
            raise InstallError("An exact supported frozen wheel version is required")

    tree: dict[str, tuple[str, bool, bool]] = {}
    directories: set[str] = set()
    output: list[PlannedWheelFile] = []
    wheels = []
    total = generated_total = 0

    def claim(path: str, directory: bool) -> None:
        _path(path + ("/" if directory else ""), limits.archive)
        if not directory or path not in directories:
            _claim(tree, path, directory, limits.archive)
        if directory:
            directories.add(path)

    def append(file: PlannedWheelFile) -> None:
        nonlocal total, generated_total
        claim(file.path, False)
        total += file.size
        generated_total += len(file.generated) if file.generated is not None else 0
        if total > limits.archive.max_total_bytes or generated_total > limits.max_generated_bytes:
            raise InstallError("Wheel plan output exceeds byte limits")
        output.append(file)

    for item in sorted(inputs, key=lambda value: _name(value.name)):
        inventory = zip_inventory(item.data, expected_sha256=item.sha256, limits=limits.archive)
        files = {file.path: file for file in inventory.files}
        roots = {path.split("/", 1)[0] for path in (*inventory.directories, *files)
                 if path.split("/", 1)[0].lower().endswith(".dist-info")}
        if len(roots) != 1:
            raise InstallError("Wheel must contain exactly one dist-info root")
        dist_info = roots.pop()
        stem = dist_info.removesuffix(".dist-info")
        name, separator, version = stem.rpartition("-")
        if not separator or _name(name) != _name(item.name) or version != item.version:
            raise InstallError("Wheel metadata directory differs from frozen identity")
        required = [dist_info + "/" + suffix for suffix in ("METADATA", "WHEEL", "RECORD")]
        if any(path not in files or files[path].size > limits.max_metadata_bytes for path in required):
            raise InstallError("Required wheel metadata is missing or exceeds limits")
        blocked = {"installer", "requested", "direct_url.json", "entry_points.txt", "record.jws", "record.p7s"}
        if any(path.startswith(dist_info + "/") and path.split("/")[1].lower() in blocked
               for path in (*inventory.directories, *files)):
            raise InstallError("Wheel requires unsupported installer metadata or script generation")
        with zipfile.ZipFile(io.BytesIO(item.data)) as source:
            metadata, wheel, record = (source.read(path) for path in required)
        metadata, wheel = _headers(metadata), _headers(wheel)
        if (_one(metadata, "Metadata-Version") not in {"1.1", "1.2", "2.0", "2.1", "2.2", "2.3", "2.4"}
                or _name(_one(metadata, "Name")) != _name(item.name) or _one(metadata, "Version") != item.version):
            raise InstallError("Wheel METADATA differs from frozen identity or supported format")
        if (_one(wheel, "Wheel-Version") != "1.0" or _one(wheel, "Root-Is-Purelib") not in {"true", "false"}
                or str(wheel.get_payload()).strip() or any(key.lower() not in
                    {"wheel-version", "root-is-purelib", "tag", "generator", "build"} for key in wheel.keys())
                or any(len(wheel.get_all(key, [])) > 1 for key in ("Generator", "Build"))):
            raise InstallError("Unsupported wheel format or transformation headers")
        wheel_tags = _tags(tuple(_values(wheel, "Tag")))
        if not set(tags).intersection(wheel_tags):
            raise InstallError("Wheel tags do not match frozen compatibility policy")
        _record(record, files, required[2])
        start = len(output)
        for path in inventory.directories:
            target = _map(path, True, dist_info)
            if target is not None:
                claim(target, True)
        for file in inventory.files:
            target = _map(file.path, False, dist_info)
            if target is None:
                raise InstallError("Wheel file has no supported destination")
            if file.path != required[2]:
                append(PlannedWheelFile(target, file.size, file.sha256, inventory.archive_sha256, file.path))
        installer_path = _SITE + dist_info + "/INSTALLER"
        append(PlannedWheelFile(installer_path, len(_INSTALLER), hashlib.sha256(_INSTALLER).hexdigest(),
                                inventory.archive_sha256, None, _INSTALLER, "installer/v1"))
        record_path = _SITE + required[2]
        rows = [(file.path.removeprefix(_SITE), _hash_field(file.sha256), str(file.size)) for file in output[start:]]
        rows.append((required[2], "", ""))
        stream = io.StringIO(newline="")
        writer = csv.writer(stream, lineterminator="\n")
        for row in sorted(rows):
            writer.writerow(row)
            if stream.tell() > limits.max_generated_bytes - generated_total:
                raise InstallError("Wheel generated metadata exceeds byte limit")
        generated = stream.getvalue().encode("ascii")
        append(PlannedWheelFile(record_path, len(generated), hashlib.sha256(generated).hexdigest(),
                                inventory.archive_sha256, None, generated, "record/v1"))
        wheels.append(PlannedWheel(_name(item.name), item.version, inventory.archive_sha256, dist_info,
                                   _one(wheel, "Root-Is-Purelib") == "true", wheel_tags, files[required[2]]))
    return WindowsWheelPlan(tuple(wheels), tags, tuple(sorted(output, key=lambda file: file.path)),
                            tuple(sorted(value[0] for value in tree.values() if value[1])))



@dataclass(frozen=True)
class WheelInputAudit:
    name: str
    version: str
    archive_sha256: str
    dist_info: str
    tags: tuple[str, ...]
    metadata_sha256: str
    inventory: ZipInventory
    metadata_version: str
    import_names: tuple[str, ...]
    import_namespaces: tuple[str, ...]
    record_validation_recipe: str


@dataclass(frozen=True)
class WindowsWheelInputInventory:
    wheels: tuple[WheelInputAudit, ...]
    schema: str = "agentstrata-windows-wheel-input-inventory/v1"


def _audit_windows_wheel_inputs(inputs: tuple[PinnedWheel, ...], *, compatible_tags: tuple[str, ...],
                               limits: WheelPlanLimits, legacy_root_boolean_case: bool = False,
                               plugin_archives: bool = False) -> WindowsWheelInputInventory:
    """Audit source archives and RECORD, without applying any installation scheme.

    Entry points and .data members stay original input bytes; their presence does
    not authorize generation, installation or execution. The old byte-plan policy
    remains deliberately stricter and is not called through this inventory API.
    """
    tags = _tags(compatible_tags, windows=True)
    if (type(limits) is not WheelPlanLimits or type(inputs) is not tuple or not inputs
            or len(inputs) > limits.max_wheels or any(type(item) is not PinnedWheel or type(item.data) is not bytes for item in inputs)):
        raise InstallError("A bounded immutable wheel input set is required")
    if sum(len(item.data) for item in inputs) > limits.archive.max_archive_bytes:
        raise InstallError("Wheel input set exceeds archive byte limit")
    names, result = set(), []
    total_bytes = total_entries = 0
    for item in sorted(inputs, key=lambda value: _name(value.name)):
        name = _name(item.name)
        if name in names or not isinstance(item.version, str) or len(item.version) > 128 or not re.fullmatch(_VERSION, item.version):
            raise InstallError("Wheel input identity repeats or has an unsupported version")
        names.add(name)
        reader = plugin_zip_inventory if plugin_archives else zip_inventory
        inventory = reader(item.data, expected_sha256=item.sha256, limits=limits.archive)
        total_bytes += sum(file.size for file in inventory.files)
        total_entries += len(inventory.files) + len(inventory.directories)
        if total_bytes > limits.archive.max_total_bytes or total_entries > limits.archive.max_tree_entries:
            raise InstallError("Wheel input inventory set exceeds expanded budgets")
        files = {file.path: file for file in inventory.files}
        roots = {path.split("/", 1)[0] for path in (*inventory.directories, *files)
                 if path.split("/", 1)[0].lower().endswith(".dist-info")}
        if len(roots) != 1:
            raise InstallError("Wheel must contain exactly one dist-info root")
        dist_info = roots.pop()
        stem, separator, version = dist_info.removesuffix(".dist-info").rpartition("-")
        if not separator or _name(stem) != name or version != item.version:
            raise InstallError("Wheel metadata directory differs from frozen identity")
        required = [dist_info + "/" + suffix for suffix in ("METADATA", "WHEEL", "RECORD")]
        if any(path not in files or files[path].size > limits.max_metadata_bytes for path in required):
            raise InstallError("Required wheel metadata is missing or exceeds limits")
        with zipfile.ZipFile(io.BytesIO(item.data)) as archive:
            metadata_raw, wheel_raw, record = (archive.read(path) for path in required)
        metadata, wheel = _headers(metadata_raw), _headers(wheel_raw)
        if (_one(metadata, "Metadata-Version") not in {"1.1", "1.2", "2.0", "2.1", "2.2", "2.3", "2.4", "2.5"}
                or _name(_one(metadata, "Name")) != name or _one(metadata, "Version") != item.version):
            raise InstallError("Wheel METADATA differs from frozen identity or supported format")
        root_values={"true","false","True","False"} if legacy_root_boolean_case else {"true","false"}
        if (_one(wheel, "Wheel-Version") != "1.0" or _one(wheel, "Root-Is-Purelib") not in root_values
                or str(wheel.get_payload()).strip() or any(key.lower() not in
                    {"wheel-version", "root-is-purelib", "tag", "generator", "build"} for key in wheel.keys())
                or any(len(wheel.get_all(key, [])) > 1 for key in ("Generator", "Build"))):
            raise InstallError("Unsupported wheel format or transformation headers")
        wheel_tags = _tags(tuple(_values(wheel, "Tag")))
        if not set(tags).intersection(wheel_tags):
            raise InstallError("Wheel tags do not match frozen compatibility policy")
        record_recipe = _input_record(record, files, required[2])
        import_names, import_namespaces = _input_import_names(metadata)
        result.append(WheelInputAudit(name, item.version, inventory.archive_sha256, dist_info,
            wheel_tags, hashlib.sha256(metadata_raw).hexdigest(), inventory,
            _one(metadata, "Metadata-Version"), import_names, import_namespaces, record_recipe))
    _check_input_import_names(result)
    return WindowsWheelInputInventory(tuple(result),schema="agentstrata-windows-mcp-wheel-input-inventory/v1"
        if legacy_root_boolean_case else "agentstrata-windows-wheel-input-inventory/v1")


def audit_windows_wheel_inputs(inputs: tuple[PinnedWheel,...], *, compatible_tags: tuple[str,...],
                              limits: WheelPlanLimits=WheelPlanLimits()) -> WindowsWheelInputInventory:
    """Original canonical header policy; never authorize installation or execution."""
    return _audit_windows_wheel_inputs(inputs,compatible_tags=compatible_tags,limits=limits)


def audit_windows_mcp_wheel_inputs(inputs: tuple[PinnedWheel,...], *, compatible_tags: tuple[str,...],
                                  limits: WheelPlanLimits=WheelPlanLimits()) -> WindowsWheelInputInventory:
    """MCP data only: additionally recognize exact legacy True/False spellings, without rewriting any archive.

    Ordinary input audit and byte installation remain strict. The distinct schema
    cannot supply a runtime/venv recipe or independent installation acceptance.
    """
    return _audit_windows_wheel_inputs(inputs,compatible_tags=compatible_tags,limits=limits,legacy_root_boolean_case=True)


def audit_windows_plugin_wheel_inputs(inputs: tuple[PinnedWheel,...], *, compatible_tags: tuple[str,...],
                                     limits: WheelPlanLimits=WheelPlanLimits()) -> WindowsWheelInputInventory:
    """Plugin data only: redundant local sizes must equal bounded ordinary ZIP32 headers."""
    value=_audit_windows_wheel_inputs(inputs,compatible_tags=compatible_tags,limits=limits,plugin_archives=True)
    return replace(value,schema='agentstrata-windows-plugin-wheel-input-inventory/v1')



def _input_import_names(metadata):
    declarations = []
    for field in ("Import-Name", "Import-Namespace"):
        values = _values(metadata, field)
        if len(values) > 128 or (values and _one(metadata, "Metadata-Version") != "2.5"):
            raise InstallError("Import metadata requires bounded Core Metadata 2.5 declarations")
        names = []
        for value in values:
            if not isinstance(value, str) or len(value) > 256 or "\n" in value or "\r" in value:
                raise InstallError("Import metadata name exceeds its syntax policy")
            parts = [part.strip() for part in value.split(";")]
            name = parts[0]
            if (len(parts) > 2 or (len(parts) == 2 and parts[1] != "private")
                    or (not name and field == "Import-Namespace")
                    or (name and not all(part.isidentifier() for part in name.split(".")))):
                raise InstallError("Import metadata name is invalid")
            names.append(name)
        if len({name.casefold() for name in names}) != len(names) or ("" in names and len(names) > 1):
            raise InstallError("Import metadata names are ambiguous")
        declarations.append(tuple(sorted(name for name in names if name)))
    if {name.casefold() for name in declarations[0]} & {name.casefold() for name in declarations[1]}:
        raise InstallError("Import name cannot be both exclusive and a namespace")
    return tuple(declarations)


def _check_input_import_names(wheels):
    entries = [(wheel.name, value.casefold(), namespace)
               for wheel in wheels for namespace, values in ((False, wheel.import_names), (True, wheel.import_namespaces))
               for value in values]
    if len(entries) > 1024:
        raise InstallError("Import metadata set exceeds its budget")
    for owner, name, namespace in entries:
        if namespace:
            continue
        for other_owner, other_name, other_namespace in entries:
            if owner == other_owner:
                continue
            overlaps = (name == other_name or other_name.startswith(name + ".")
                        or (not other_namespace and name.startswith(other_name + ".")))
            if overlaps:
                raise InstallError("Frozen distributions declare conflicting import ownership")



def _input_record(data, files, record_path):
    """Interpret Windows RECORD separators without changing any archive bytes.

    Only the CSV path column is normalized. The existing strict complete-set,
    SHA256, size and duplicate checks apply to the normalized paths. The original
    installation planner keeps its original RECORD policy.
    """
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    normalized = False
    try:
        for row in csv.reader(io.StringIO(data.decode("utf-8"), newline=""), strict=True):
            if len(row) != 3:
                raise InstallError("Wheel RECORD requires exactly three columns")
            path = row[0].replace("\\", "/")
            normalized |= path != row[0]
            writer.writerow((path, row[1], row[2]))
    except (UnicodeError, csv.Error):
        raise InstallError("Invalid UTF-8 wheel RECORD CSV") from None
    _record(stream.getvalue().encode("utf-8"), files, record_path)
    return "windows-record-separators/v1" if normalized else "record/v1"


def locked_wheel_artifact(row, name, version):
    if (type(row) is not dict or type(row.get("url")) is not str or not row["url"].isascii()
            or len(row["url"]) > 2048 or type(row.get("size")) is not int
            or not 0 < row["size"] <= 128 * 1024 * 1024
            or type(row.get("hash")) is not str or not re.fullmatch(r"sha256:[a-f0-9]{64}", row["hash"])):
        raise ValueError("Native wheel artifact fields exceed their fixed-input policy")
    try:
        url = urlsplit(row["url"])
        port = url.port
    except ValueError:
        raise ValueError("Native wheel URL has an invalid authority") from None
    filename = url.path.rsplit("/", 1)[-1]
    if (url.scheme != "https" or not url.hostname or port not in {None, 443}
            or url.username or url.password or url.query or url.fragment
            or len(filename) > 240 or not re.fullmatch(r"[A-Za-z0-9_.!+\-]+\.whl", filename)):
        raise ValueError("Native wheel requires an unambiguous credential-free HTTPS filename")
    distribution, parsed_version, build, tags = parse_wheel_filename(filename)
    if distribution != name or parsed_version != Version(version):
        raise ValueError("Native wheel filename differs from locked distribution identity")
    return {"name": name, "version": version, "filename": filename, "url": row["url"],
            "sha256": row["hash"].removeprefix("sha256:"), "bytes": row["size"]}, build or (0, ""), tags

