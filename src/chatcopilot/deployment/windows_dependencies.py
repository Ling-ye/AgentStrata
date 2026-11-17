"""Bind native host dependency declarations to delivered source, without installing."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import tomllib
from urllib.parse import urlsplit

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.version import Version

from chatcopilot.core.file_integrity import trusted_source_sha256
from chatcopilot.core.source_snapshot import manifest_digest

MEMBER = "windows-dependency-declarations.json"
HOST_LOCK = "integrations/live2d_desktop/windows-host-requirements.lock"
RUNTIME_LOCK = "integrations/live2d_desktop/windows-runtime.lock.json"
INPUT_LIMITS = {"pyproject.toml": 256 * 1024, "uv.lock": 8 * 1024 * 1024,
                HOST_LOCK: 4 * 1024 * 1024, RUNTIME_LOCK: 64 * 1024}
EXTRAS = ("agent",)
UV_VERSION = "0.12.5"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate native dependency declaration key")
            result[key] = value
        return result
    return json.loads(data, object_pairs_hook=unique)


def canonical(data) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode() + b"\n"


def _requirement(value: str) -> Requirement:
    if (type(value) is not str or not value or len(value) > 4096 or not value.isascii()
            or any(char in value for char in "\r\n\x00") or value.count("(") > 16
            or value.count("[") > 32):
        raise ValueError("Native dependency requirement exceeds its syntax budget")
    try:
        parsed = Requirement(value)
    except (ValueError, RecursionError):
        raise ValueError("Invalid native dependency requirement") from None
    if parsed.url is not None:
        raise ValueError("Native dependency declarations require named registry dependencies")
    return parsed


def canonical_requirements(raw: bytes) -> bytes:
    """Ignore annotations, retain every requirement, marker and SHA256 pin in order."""
    try:
        text = raw.decode("ascii")
    except UnicodeError:
        raise ValueError("Native dependency lock must contain ASCII requirements") from None
    lines: list[str] = []
    pending: list[str] = []
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if len(line) > 8192 or "\x00" in line:
            raise ValueError("Native dependency lock line exceeds its budget")
        continuation = line.endswith("\\")
        item = line[:-1].rstrip() if continuation else line.rstrip()
        if not pending:
            requirement = _requirement(item.strip())
            specs = tuple(requirement.specifier)
            if len(specs) != 1 or specs[0].operator != "==" or "*" in specs[0].version:
                raise ValueError("Native dependency lock requires exact named versions")
        elif not re.fullmatch(r"    --hash=sha256:[a-f0-9]{64}", item):
            raise ValueError("Native dependency lock contains an unsupported option or hash")
        pending.append(item + (" \\" if continuation else ""))
        if not continuation:
            if len(pending) < 2:
                raise ValueError("Native dependency lock requires artifact hashes")
            lines.extend(pending)
            pending = []
    if pending or not lines:
        raise ValueError("Native dependency lock is empty or incomplete")
    return ("\n".join(lines) + "\n").encode("ascii")


def _read_inputs(root: Path, manifest=None, *, include_host_lock=True):
    root = root.absolute()
    data = {}
    for name, limit in INPUT_LIMITS.items():
        if name == HOST_LOCK and not include_host_lock:
            continue
        path = root / name
        observed = trusted_source_sha256(path, root=root, max_bytes=limit)
        expected = manifest[name]["sha256"] if manifest is not None and name in manifest else observed
        if manifest is not None and name not in manifest:
            raise ValueError("Delivered source lacks a native dependency input")
        with path.open("rb") as stream:
            content = stream.read(limit + 1)
        if len(content) > limit or _sha(content) != expected or observed != expected:
            raise ValueError("Native dependency source differs from its frozen input")
        data[name] = content
    return data


def _declarations(data, source_digest, *, check_host_lock=True):
    try:
        project = tomllib.loads(data["pyproject.toml"].decode())["project"]
        metadata = tomllib.loads(data["pyproject.toml"].decode())
        lock = tomllib.loads(data["uv.lock"].decode())
        runtime = _json(data[RUNTIME_LOCK])
        core = project["dependencies"]
        groups = project["optional-dependencies"]
        roots = [*core, *(item for extra in EXTRAS for item in groups[extra])]
        version = runtime["host_python"]
        if (project["name"] != "agentstrata" or project.get("dynamic") or not core
                or not all(type(groups[extra]) is list and groups[extra] for extra in EXTRAS)
                or type(core) is not list or not 0 < len(roots) <= 128
                or type(project["version"]) is not str or not 0 < len(project["version"]) <= 128
                or metadata.get("tool", {}).get("uv")
                or runtime["schema"] != "agentstrata-windows-runtime-inputs/v1"
                or type(version) is not str or len(version) > 32 or not re.fullmatch(r"3\.(12|13)\.[0-9]+", version)
                or not SpecifierSet(project["requires-python"]).contains(Version(version))):
            raise ValueError("Unsupported native application dependency declarations")
        for item in roots:
            _requirement(item)
        Version(project["version"])
        packages = lock["package"]
        if type(packages) is not list or not 0 < len(packages) <= 1024:
            raise ValueError("Native dependency lock package set exceeds its budget")
        local = []
        for package in packages:
            source = package["source"]
            if source == {"editable": "."}:
                local.append(package["name"])
            else:
                if type(source) is not dict or set(source) != {"registry"}:
                    raise ValueError("Native dependency lock contains an unsupported source")
                url = urlsplit(source["registry"])
                if url.scheme != "https" or not url.hostname or url.username or url.password or url.fragment or url.query:
                    raise ValueError("Native dependency registry must use credential-free HTTPS")
        if local != [project["name"]]:
            raise ValueError("Native dependency lock requires one local application")
        requirements = canonical_requirements(data[HOST_LOCK]) if check_host_lock else b""
    except (KeyError, TypeError, ValueError, RecursionError):
        raise ValueError("Malformed native application dependency inputs") from None
    return {"schema": "agentstrata-windows-dependency-declarations/v1", "state": "declared",
        "source_digest": source_digest,
        "inputs": {name: _sha(raw) for name, raw in sorted(data.items())},
        "project": {"name": project["name"], "version": project["version"], "requires_python": project["requires-python"]},
        "target": {"platform": "windows-x86_64", "python_version": version},
        "extras": list(EXTRAS), "requirements": list(dict.fromkeys(roots)),
        "locked_requirements_sha256": _sha(requirements),
        "export_recipe": "uv-0.12.5-locked-offline-agent/v1", "scope": "application-host-declarations",
        "wheel_closure_verified": False, "native_abi_verified": False, "installation_accepted": False}


def compiler_path() -> Path:
    """Use host tool configuration, never a candidate-controlled executable or PATH."""
    return Path(os.environ.get("CHATCOPILOT_HARNESS_UV_BIN", str(
        Path.home() / ".local/share/agentstrata/uv/0.12.5/bin/uv")))


def _export(data, uv: Path) -> bytes:
    uv = uv.absolute()
    if not uv.is_file():
        raise ValueError("Native dependency validation requires the host uv 0.12.5 tool")
    with tempfile.TemporaryDirectory(prefix="agentstrata-dependency-export-") as directory:
        stage = Path(directory)
        for name in ("pyproject.toml", "uv.lock"):
            (stage / name).write_bytes(data[name])
        environment = {"PATH": os.defpath, "HOME": str(stage / "home"), "LANG": "C.UTF-8"}
        if os.name == "nt":
            environment["SystemRoot"] = os.environ["SystemRoot"]
            environment["TEMP"] = str(stage)
            environment["TMP"] = str(stage)
        def run(arguments):
            return subprocess.run(arguments, cwd=stage, env=environment, capture_output=True, timeout=30, check=True)
        try:
            version = run([str(uv), "--version"]).stdout.decode("ascii").strip()
            if not re.fullmatch(r"uv 0\.12\.5(?: \([^\r\n]+\))?", version):
                raise ValueError("Native dependency export requires uv 0.12.5")
            result = run([str(uv), "export", "--locked", "--offline", "--no-config", "--no-cache",
                "--no-default-groups", "--no-dev", "--extra", "agent", "--no-emit-project", "--no-header",
                "--no-annotate", "--format", "requirements-txt", "--no-build", "--no-python-downloads",
                "--python", sys.executable])
        except (OSError, subprocess.SubprocessError, UnicodeError):
            raise ValueError("Native dependency export failed; check the frozen project and uv.lock offline") from None
        if len(result.stdout) > INPUT_LIMITS[HOST_LOCK]:
            raise ValueError("Native dependency export exceeds its byte budget")
        if (stage / "uv.lock").read_bytes() != data["uv.lock"]:
            raise ValueError("Native dependency export changed its frozen lock")
        return canonical_requirements(result.stdout)


def checked_host_requirements(root: Path, *, uv: Path | None = None) -> bytes:
    """Check local declaration consistency; this is not a provenance or runtime receipt."""
    data = _read_inputs(root, include_host_lock=False)
    _declarations(data, "0" * 64, check_host_lock=False)
    return _export(data, uv if uv is not None else compiler_path())


def build_dependency_declarations(source: Path, manifest: dict, *, uv: Path | None = None) -> bytes:
    data = _read_inputs(source, manifest)
    result = _declarations(data, manifest_digest(manifest))
    if _export(data, uv if uv is not None else compiler_path()) != canonical_requirements(data[HOST_LOCK]):
        raise ValueError("Native host requirements differ from the frozen project; regenerate the native lock")
    return canonical(result)


def verify_dependency_declarations(source: Path, manifest: dict, raw: bytes) -> None:
    if type(raw) is not bytes or len(raw) > 256 * 1024:
        raise ValueError("Native dependency declaration artifact exceeds its budget")
    expected = _declarations(_read_inputs(source, manifest), manifest_digest(manifest))
    if raw != canonical(expected):
        raise ValueError("Native dependency declarations differ from the delivered source")
