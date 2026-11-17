"""Consume host-pinned Python inputs and derive a Windows copy plan in memory.

Neither a local JSON file nor a returned plan is an acceptance or launch receipt.
The expected input-lock digest must be independently frozen by the trusted host.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from urllib.parse import urlsplit

from chatcopilot.core.archive_inventory import ZipInventoryLimits, _claim, _path
from chatcopilot.core.install_artifacts import InstallError
from chatcopilot.core.tar_inventory import TarInventoryLimits, tar_inventory

_SLOTS = {"host_python": "host", "vtuber_python": "vtuber"}
_MCP_LANE = "windows_mcp_python"
_V2_SLOTS = {**_SLOTS, _MCP_LANE: "windows-mcp"}
_SOURCE_PATH = "crates/uv-python/download-metadata.json"


@dataclass(frozen=True)
class PythonPlanLimits:
    archive: TarInventoryLimits = TarInventoryLimits()
    max_inputs_bytes: int = 64 * 1024
    max_metadata_bytes: int = 8 * 1024 * 1024
    max_json_depth: int = 16
    max_json_tokens: int = 500_000

    def __post_init__(self):
        if type(self.archive) is not TarInventoryLimits or any(type(value) is not int or value <= 0 for value in
                (self.max_inputs_bytes, self.max_metadata_bytes, self.max_json_depth, self.max_json_tokens)):
            raise InstallError("Python plan limits require positive integers and TAR limits")


@dataclass(frozen=True)
class PythonArchivePin:
    lane: str
    version: str
    selector: str
    build: str
    url: str
    sha256: str
    root: str
    format: str


@dataclass(frozen=True)
class PythonInputs:
    inputs_sha256: str
    metadata_sha256: str
    source_commit: str
    source_blob_sha1: str
    archives: tuple[PythonArchivePin, ...]
    schema: str = "agentstrata-resolved-python-inputs/v1"


@dataclass(frozen=True)
class PlannedPythonFile:
    path: str
    source_path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class WindowsPythonPlan:
    inputs_sha256: str
    metadata_sha256: str
    source_commit: str
    pin: PythonArchivePin
    tar_sha256: str
    tar_bytes: int
    target_root: str
    files: tuple[PlannedPythonFile, ...]
    directories: tuple[str, ...]
    recipe: str = "windows-standalone-copy/v1"
    schema: str = "agentstrata-windows-python-plan/v1"


def _digest(value, digits: int = 64) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{" + str(digits) + "}", value):
        raise InstallError("A canonical frozen Python digest is required")
    return value


def _bytes(value: bytes, limit: int) -> None:
    if type(value) is not bytes:
        raise InstallError("Python inputs require immutable bytes")
    if len(value) > limit:
        raise InstallError("Python input bytes exceed limits")


def _json(data: bytes, limits: PythonPlanLimits) -> dict:
    # Bound container depth and structural work before JSON object allocation.
    depth = tokens = 0
    quoted = escaped = False
    for byte in data:
        if quoted:
            if escaped:
                escaped = False
            elif byte == 92:
                escaped = True
            elif byte == 34:
                quoted = False
        elif byte == 34:
            quoted = True
            tokens += 1
        elif byte in (123, 91):
            depth += 1
            tokens += 1
        elif byte in (125, 93):
            depth -= 1
        elif byte == 44:
            tokens += 1
        if depth < 0 or depth > limits.max_json_depth or tokens > limits.max_json_tokens:
            raise InstallError("Python JSON structure exceeds limits")
    if depth or quoted:
        raise InstallError("Incomplete Python JSON structure")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise InstallError("Duplicate Python JSON key")
            result[key] = value
        return result

    def integer(value):
        if len(value) > 20:
            raise InstallError("Python JSON integer exceeds limit")
        return int(value)

    def unsupported(_value):
        raise InstallError("Python JSON floats and constants are unsupported")

    try:
        result = json.loads(data.decode("utf-8"), object_pairs_hook=pairs, parse_int=integer,
                            parse_float=unsupported, parse_constant=unsupported)
    except (UnicodeError, ValueError, RecursionError):
        raise InstallError("Invalid Python JSON input") from None
    if type(result) is not dict:
        raise InstallError("Python JSON input must be an object")
    return result


def _keys(value, names: set[str]) -> dict:
    if type(value) is not dict or set(value) != names:
        raise InstallError("Python input schema fields differ")
    return value


def _version(value) -> tuple[int, int, int]:
    if not isinstance(value, str) or not re.fullmatch(r"3\.(?:12|13)\.(?:0|[1-9][0-9]{0,3})", value):
        raise InstallError("Only stable Windows CPython 3.12/3.13 inputs are supported")
    major, minor, patch = map(int, value.split("."))
    return major, minor, patch


def _archive_version(value, lane: str) -> tuple[int, int, int]:
    # Supporting an inert MCP base-image plan does not admit a 3.14 venv/ABI recipe.
    if lane != _MCP_LANE:
        return _version(value)
    if type(value) is not str or not re.fullmatch(r"3\.14\.(?:0|[1-9][0-9]{0,3})", value):
        raise InstallError("Windows-MCP archive lane requires stable CPython 3.14")
    major, minor, patch = map(int, value.split("."))
    return major, minor, patch


def _archive_url(value, version: str, build: str) -> str:
    if not isinstance(value, str):
        raise InstallError("Python release URL is invalid")
    try:
        parsed = urlsplit(value)
    except ValueError:
        raise InstallError("Python release URL is invalid") from None
    path = ("/astral-sh/python-build-standalone/releases/download/" + build + "/cpython-" + version
            + "%2B" + build + "-x86_64-pc-windows-msvc-install_only_stripped.tar.gz")
    if (parsed.scheme != "https" or parsed.netloc != "github.com" or parsed.path != path
            or parsed.query or parsed.fragment or value != "https://" + parsed.netloc + parsed.path):
        raise InstallError("Python release URL differs from the frozen Windows input")
    return value


def resolve_python_inputs(inputs: bytes, metadata: bytes, *, expected_inputs_sha256: str,
                          limits: PythonPlanLimits = PythonPlanLimits()) -> PythonInputs:
    """Resolve only an independently pinned lock and its complete supplier table.

    This function performs no downloads and never hashes an installed environment
    to establish trust. Returned URLs and pins are facts from verified inputs.
    """
    _bytes(inputs, limits.max_inputs_bytes)
    inputs_digest = hashlib.sha256(inputs).hexdigest()
    if inputs_digest != _digest(expected_inputs_sha256):
        raise InstallError("Python input lock differs from the independent host pin")
    document = _keys(_json(inputs, limits), {"schema", "source", "archives"})
    if document["schema"] not in ("agentstrata-windows-python-inputs/v1", "agentstrata-windows-python-inputs/v2"):
        raise InstallError("Unsupported Python input schema")
    source = _keys(document["source"], {"repository", "tag", "commit", "url", "git_blob_sha1", "sha256", "bytes"})
    commit, blob, metadata_digest = _digest(source["commit"], 40), _digest(source["git_blob_sha1"], 40), _digest(source["sha256"])
    if (source["repository"] != "astral-sh/uv" or not isinstance(source["tag"], str)
            or not re.fullmatch(r"[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}", source["tag"])
            or source["url"] != "https://raw.githubusercontent.com/astral-sh/uv/" + commit + "/" + _SOURCE_PATH
            or type(source["bytes"]) is not int or not 0 < source["bytes"] <= limits.max_metadata_bytes):
        raise InstallError("Python supplier source is not a fixed supported input")
    _bytes(metadata, limits.max_metadata_bytes)
    if len(metadata) != source["bytes"] or hashlib.sha256(metadata).hexdigest() != metadata_digest:
        raise InstallError("Python supplier metadata differs from frozen input")
    if hashlib.sha1(b"blob " + str(len(metadata)).encode("ascii") + b"\0" + metadata).hexdigest() != blob:
        raise InstallError("Python supplier Git blob differs from frozen input")
    table = _json(metadata, limits)
    slots = _SLOTS if document["schema"] == "agentstrata-windows-python-inputs/v1" else _V2_SLOTS
    entries = _keys(document["archives"], set(slots))
    output = []
    for lane in sorted(entries):
        entry = _keys(entries[lane], {"version", "selector", "sha256", "build", "format", "root"})
        version = _archive_version(entry["version"], lane)
        selector = "cpython-" + entry["version"] + "-windows-x86_64-none"
        if (entry["selector"] != selector or not isinstance(entry["build"], str)
                or not re.fullmatch(r"[0-9]{8}", entry["build"]) or entry["root"] != "python" or entry["format"] != "tar.gz"):
            raise InstallError("Python archive selector or layout differs from supported input")
        digest = _digest(entry["sha256"])
        row = table.get(selector)
        if (type(row) is not dict or row.get("name") != "cpython" or row.get("os") != "windows"
                or row.get("arch") != {"family": "x86_64", "variant": None} or row.get("libc") != "none"
                or row.get("variant", "missing") is not None or row.get("prerelease") != ""
                or any(type(row.get(field)) is not int for field in ("major", "minor", "patch"))
                or tuple(row.get(field) for field in ("major", "minor", "patch")) != version
                or row.get("build") != entry["build"] or row.get("sha256") != digest):
            raise InstallError("Python supplier row differs from frozen archive identity")
        url = _archive_url(row.get("url"), entry["version"], entry["build"])
        output.append(PythonArchivePin(lane, entry["version"], selector, entry["build"], url, digest, "python", "tar.gz"))
    return PythonInputs(inputs_digest, metadata_digest, commit, blob, tuple(output),
        schema="agentstrata-resolved-python-inputs/v1" if slots is _SLOTS else "agentstrata-resolved-python-inputs/v2")


def windows_python_plan(inputs: bytes, metadata: bytes, archive: bytes, *, expected_inputs_sha256: str,
                        lane: str, limits: PythonPlanLimits = PythonPlanLimits()) -> WindowsPythonPlan:
    """Map every pinned base-image file into a fixed content-addressed slot.

    No target path, installed-state manifest or resolved-pin object is accepted.
    A future provisioner must separately establish ownership, safe writes, venv
    recipes, loader isolation, ABI compatibility and independent acceptance.
    """
    if type(lane) is not str or lane not in _V2_SLOTS:
        raise InstallError("A fixed Python runtime lane is required")
    resolved = resolve_python_inputs(inputs, metadata, expected_inputs_sha256=expected_inputs_sha256, limits=limits)
    pin = next((item for item in resolved.archives if item.lane == lane), None)
    if pin is None:
        raise InstallError("Python runtime lane is absent from the independent input lock")
    inventory = tar_inventory(archive, expected_sha256=pin.sha256, compression="gzip", limits=limits.archive)
    target_root = "runtime/python/" + _V2_SLOTS[lane] + "/" + pin.sha256
    version = _archive_version(pin.version, lane)
    abi_dll = f"python{version[0]}{version[1]}.dll"
    required = {"python.exe", "pythonw.exe", "python3.dll", abi_dll, "vcruntime140.dll", "vcruntime140_1.dll",
                "LICENSE.txt", "Lib/os.py", "Lib/site.py", "Lib/encodings/__init__.py"}
    members = {file.path: file for file in inventory.files}
    if any(pin.root + "/" + name not in members or not members[pin.root + "/" + name].size for name in required):
        raise InstallError("Required Python base-image files are missing or empty")
    path_limits = ZipInventoryLimits(max_path_bytes=limits.archive.max_path_bytes, max_depth=limits.archive.max_depth,
                                     max_tree_entries=limits.archive.max_tree_entries)
    tree: dict[str, tuple[str, bool, bool]] = {}

    def mapped(path: str, directory: bool) -> str:
        if path == pin.root and directory:
            target = target_root
        elif path.startswith(pin.root + "/"):
            relative = path[len(pin.root) + 1:]
            parts = relative.lower().split("/")
            if (any(part in {"sitecustomize", "usercustomize"} or part.startswith(("sitecustomize.", "usercustomize.")) for part in parts)
                    or parts[-1] == "pyvenv.cfg" or parts[-1].endswith((".pth", "._pth"))):
                raise InstallError("Python base image contains unsupported startup configuration")
            if len(parts) == 1 and re.fullmatch(r"python.*\.dll", parts[0]) and relative not in {"python3.dll", abi_dll}:
                raise InstallError("Python base image contains an unexpected interpreter DLL")
            target = target_root + "/" + relative
        else:
            raise InstallError("Python archive contains a member outside its fixed root")
        _path(target + ("/" if directory else ""), path_limits)
        _claim(tree, target, directory, path_limits)
        return target

    for path in inventory.directories:
        mapped(path, True)
    output = tuple(PlannedPythonFile(mapped(file.path, False), file.path, file.size, file.sha256) for file in inventory.files)
    return WindowsPythonPlan(resolved.inputs_sha256, resolved.metadata_sha256, resolved.source_commit, pin,
                             inventory.tar_sha256, inventory.tar_bytes, target_root,
                             tuple(sorted(output, key=lambda file: file.path)),
                             tuple(sorted(value[0] for value in tree.values() if value[1])))
