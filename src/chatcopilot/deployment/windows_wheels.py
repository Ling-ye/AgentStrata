"""Freeze and verify native host wheel inputs without installing or importing them."""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import os
import tomllib

from packaging.utils import canonicalize_name, parse_wheel_filename

from chatcopilot.core.dependency_closure import (
    windows_dependency_input_inventory, windows_python_tags, windows_requirement_fact,
)
from chatcopilot.core.file_integrity import trusted_source_sha256
from chatcopilot.core.python_environment import canonical_digest
from chatcopilot.core.wheel_installation import windows_wheel_installation_plan
from chatcopilot.core.source_snapshot import manifest_digest
from chatcopilot.core.wheel_plan import PinnedWheel, locked_wheel_artifact
from chatcopilot.deployment.windows_dependencies import (
    HOST_LOCK, _declarations, _json, _read_inputs, _sha, canonical, canonical_requirements,
)

MEMBER = "windows-host-wheel-inputs.json"
VERIFIED = "windows-host-wheel-verification.json"
REQUIREMENTS = "host-requirements.lock"
DIRECTORY = "host-wheels"
LAYOUT = "windows-host-wheel-layout.json"
ENVIRONMENT_INPUTS = "windows-host-environment-inputs.json"
PYTHON_INPUTS = "integrations/live2d_desktop/windows-python-inputs.lock.json"
CACHE_VARIABLE = "AGENTSTRATA_NATIVE_HOST_WHEELHOUSE"
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_WHEEL_BYTES = 128 * 1024 * 1024


def _rows(raw):
    rows: list[tuple[str, set[str]]] = []
    for line in canonical_requirements(raw).decode("ascii").splitlines():
        if not line.startswith(" "):
            rows.append((line.removesuffix(" \\"), set()))
        else:
            rows[-1][1].add(line.strip().removesuffix(" \\").removeprefix("--hash=sha256:"))
    if not 0 < len(rows) <= 256:
        raise ValueError("Native host wheel requirement set exceeds its budget")
    return rows


def _artifact(row, name, version):
    return locked_wheel_artifact(row, name, version)


def _selection(data, source_digest):
    declaration = _declarations(data, source_digest)
    version = declaration["target"]["python_version"]
    target_tags = windows_python_tags(version)
    ranks = {tag: index for index, tag in enumerate(target_tags)}
    packages = tomllib.loads(data["uv.lock"].decode())["package"]
    if sum(len(package.get("wheels", [])) for package in packages) > 16384:
        raise ValueError("Native wheel candidate table exceeds its budget")
    selected, inactive, names = [], [], set()
    for text, allowed_hashes in _rows(data[HOST_LOCK]):
        fact = windows_requirement_fact(text, python_version=version)
        if not fact.active:
            inactive.append(fact.requirement_sha256)
            continue
        if fact.name in names or fact.requested_extras:
            raise ValueError("Native host lock has ambiguous active distributions or unresolved extras")
        names.add(fact.name)
        matches = [package for package in packages if canonicalize_name(package["name"], validate=True) == fact.name
                   and package.get("version") == fact.specifier.removeprefix("==")]
        if len(matches) != 1:
            raise ValueError("Native host requirement does not select one frozen distribution")
        package = matches[0]
        candidates, filenames = [], set()
        for row in package.get("wheels", []):
            artifact, build, tags = _artifact(row, fact.name, package["version"])
            if artifact["filename"] in filenames:
                raise ValueError("Native wheel candidate filenames repeat")
            filenames.add(artifact["filename"])
            overlap = [ranks[str(tag)] for tag in tags if str(tag) in ranks]
            if overlap:
                if artifact["sha256"] not in allowed_hashes:
                    raise ValueError("Compatible native wheel is absent from the host requirement hash set")
                candidates.append((min(overlap), build, artifact))
        if not candidates:
            raise ValueError("Native host distribution lacks a compatible locked wheel: " + fact.name)
        rank = min(row[0] for row in candidates)
        build = max(row[1] for row in candidates if row[0] == rank)
        best = [row[2] for row in candidates if row[:2] == (rank, build)]
        if len(best) != 1:
            raise ValueError("Native wheel preference is ambiguous")
        selected.append(best[0])
    if not selected or sum(row["bytes"] for row in selected) > MAX_ARCHIVE_BYTES:
        raise ValueError("Native host wheel set exceeds its archive budget")
    return {"schema": "agentstrata-windows-host-wheel-inputs/v1", "state": "selected",
        "source_digest": source_digest, "inputs": declaration["inputs"], "target": declaration["target"],
        "requirements": declaration["requirements"], "extras": declaration["extras"],
        "recipe": "windows-x64-cpython-wheel-selection/v1", "compatible_tags": list(target_tags),
        "inactive_requirement_sha256": sorted(inactive), "wheels": sorted(selected, key=lambda row: row["name"]),
        "scope": "application-host-wheel-inputs", "native_abi_verified": False, "installation_accepted": False}


def host_wheel_inputs(source: Path, manifest: dict) -> dict:
    return _selection(_read_inputs(source, manifest), manifest_digest(manifest))


def _cached_inputs(cache: Path, plan):
    if not cache.is_absolute():
        raise ValueError("Host wheel cache requires an absolute host-owned directory")
    result = []
    for row in plan["wheels"]:
        path = cache / row["filename"]
        if trusted_source_sha256(path, root=cache, max_bytes=row["bytes"]) != row["sha256"]:
            raise ValueError("Cached host wheel differs from its source pin")
        with path.open("rb") as stream:
            raw = stream.read(row["bytes"] + 1)
        if len(raw) != row["bytes"] or _sha(raw) != row["sha256"]:
            raise ValueError("Cached host wheel differs from its source size or pin")
        result.append(PinnedWheel(raw, row["sha256"], row["name"], row["version"]))
    return tuple(result)


def _verification(plan, pins, *, mapped=False):
    checked = windows_dependency_input_inventory(pins, python_version=plan["target"]["python_version"],
        requirements=tuple(plan["requirements"]))
    by_name = {row["name"]: row for row in plan["wheels"]}
    audits = []
    for wheel in checked.inventory.wheels:
        expected_tags = parse_wheel_filename(by_name[wheel.name]["filename"])[3]
        if set(wheel.tags) != {str(tag) for tag in expected_tags}:
            raise ValueError("Wheel WHEEL tags differ from the selected archive filename")
        files = wheel.inventory.files
        audits.append({"name": wheel.name, "version": wheel.version, "archive_sha256": wheel.archive_sha256,
            "metadata_version": wheel.metadata_version, "import_names": list(wheel.import_names),
            "import_namespaces": list(wheel.import_namespaces), "record_validation_recipe": wheel.record_validation_recipe,
            "metadata_sha256": wheel.metadata_sha256, "inventory_sha256": _sha(canonical(asdict(wheel.inventory))),
            "files": len(files), "expanded_bytes": sum(file.size for file in files),
            "entry_points": [asdict(file) for file in files if file.path == wheel.dist_info + "/entry_points.txt"],
            "native_files": [asdict(file) for file in files if file.path.lower().endswith((".pyd", ".dll"))],
            "data_members": [asdict(file) for file in files if ".data/" in file.path]})
    result = {"schema": "agentstrata-windows-host-wheel-verification/v1", "state": "verified-inputs",
        "input_manifest_sha256": _sha(canonical(plan)), "closure": asdict(checked.closure), "archives": audits,
        "scope": "application-host-wheel-inputs", "installation_mapping_verified": False,
        "declared_import_conflicts_checked": True, "full_import_ownership_verified": False,
        "native_abi_verified": False, "installation_accepted": False}
    if mapped:
        result["mapped_inputs"] = {"layout_schema":"agentstrata-windows-host-wheel-layout/v1",
            "environment_inputs_schema":"agentstrata-windows-python-environment-inputs/v3"}
    return result


def _requirements(plan):
    lines = ["# Fixed host wheel inputs; generated from frozen source.\n"]
    for row in plan["wheels"]:
        lines.append(row["name"] + "==" + row["version"] + " \\\n    --hash=sha256:" + row["sha256"] + "\n")
    return "".join(lines).encode("ascii")


def reserved_member(name: str) -> bool:
    return name in {MEMBER, VERIFIED, REQUIREMENTS, LAYOUT, ENVIRONMENT_INPUTS} or name == DIRECTORY or name.startswith(DIRECTORY + "/")


def prepare_host_wheels(payload: Path, manifest: dict, *, cache: Path | None = None) -> dict:
    """Emit selected pins; when a complete cache is supplied audit and copy exact bytes.

    No automatic download, cache repair, package installation or execution occurs.
    A cache failure stops before any host wheel payload is written.
    """
    plan = host_wheel_inputs(payload / "source", manifest)
    if cache is None and os.environ.get(CACHE_VARIABLE):
        cache = Path(os.environ[CACHE_VARIABLE])
    rows = {}
    if cache is not None:
        pins = _cached_inputs(cache, plan)
        verification = _verification(plan, pins, mapped=True)
        layout = _layout(plan, pins)
        environment_inputs = _environment_inputs(payload / "source", manifest, plan)
        directory = payload / DIRECTORY
        directory.mkdir(exist_ok=False)
        by_name = {pin.name: pin for pin in pins}
        for row in plan["wheels"]:
            name = DIRECTORY + "/" + row["filename"]
            with (payload / name).open("xb") as stream:
                stream.write(by_name[row["name"]].data)
            rows[name] = row["sha256"]
        raw = canonical(verification)
        with (payload / VERIFIED).open("xb") as stream:
            stream.write(raw)
        rows[VERIFIED] = _sha(raw)
        raw = _requirements(plan)
        with (payload / REQUIREMENTS).open("xb") as stream:
            stream.write(raw)
        rows[REQUIREMENTS] = _sha(raw)
        for name, raw in ((LAYOUT, canonical(layout)), (ENVIRONMENT_INPUTS, environment_inputs)):
            with (payload / name).open("xb") as stream:
                stream.write(raw)
            rows[name] = _sha(raw)
    raw = canonical(plan)
    with (payload / MEMBER).open("xb") as stream:
        stream.write(raw)
    rows[MEMBER] = _sha(raw)
    return rows


def _read(path, limit):
    with path.open("rb") as stream:
        return stream.read(limit + 1)


def verify_host_wheels(payload: Path, manifest: dict) -> None:
    plan = host_wheel_inputs(payload / "source", manifest)
    if _read(payload / MEMBER, 256 * 1024) != canonical(plan):
        raise ValueError("Native host wheel selection differs from frozen source")
    if (payload / VERIFIED).exists():
        if set(path.name for path in (payload / DIRECTORY).iterdir()) != {row["filename"] for row in plan["wheels"]}:
            raise ValueError("Native host wheel directory contains missing or extra members")
        pins = _cached_inputs(payload / DIRECTORY, plan)
        report = _read(payload / VERIFIED, 8 * 1024 * 1024)
        mapped = report == canonical(_verification(plan, pins, mapped=True))
        if not mapped and report != canonical(_verification(plan, pins)):
            raise ValueError("Native host wheel verification differs from original inputs")
        if _read(payload / REQUIREMENTS, 256 * 1024) != _requirements(plan):
            raise ValueError("Native host requirements differ from selected single-hash inputs")
        present = ((payload / LAYOUT).is_file(), (payload / ENVIRONMENT_INPUTS).is_file())
        if mapped:
            if not all(present):
                raise ValueError("Native host mapped layout and environment inputs must both be present")
            if (_read(payload / LAYOUT, 16 * 1024 * 1024) != canonical(_layout(plan, pins))
                    or _read(payload / ENVIRONMENT_INPUTS, 64 * 1024) != _environment_inputs(payload / "source", manifest, plan)):
                raise ValueError("Native host mapped layout or environment inputs differ from source")
        elif any(present):
            raise ValueError("Legacy host wheel report cannot authorize mapped environment inputs")
    elif any((payload / name).exists() for name in (DIRECTORY, REQUIREMENTS, LAYOUT, ENVIRONMENT_INPUTS)):
        raise ValueError("Native host wheel payload is incomplete")



def _layout(plan, pins):
    mapped = windows_wheel_installation_plan(pins, python_version=plan["target"]["python_version"],
        requirements=tuple(plan["requirements"]))
    files = [{key:value for key,value in asdict(file).items() if key != "generated"} for file in mapped.files]
    return {"schema":"agentstrata-windows-host-wheel-layout/v1", "state":"planned",
        "input_manifest_sha256":_sha(canonical(plan)), "mapping_plan_sha256":canonical_digest(asdict(mapped)),
        "recipe":mapped.recipe,"files":files,"directories":mapped.directories,
        "entry_points":asdict(mapped)["entry_points"],"deferred_files":asdict(mapped)["deferred_files"],
        "site_import":False,"runtime_prefix_verified":False,"native_abi_verified":False,"installation_accepted":False}


def _environment_inputs(source, manifest, plan):
    if PYTHON_INPUTS not in manifest:
        raise ValueError("Mapped host layout requires its frozen Python input lock")
    path=source/PYTHON_INPUTS
    expected=manifest[PYTHON_INPUTS]["sha256"]
    if trusted_source_sha256(path,root=source,max_bytes=64*1024) != expected:
        raise ValueError("Host Python input lock differs from frozen source")
    raw=_read(path,64*1024)
    if len(raw)>64*1024 or _sha(raw)!=expected:
        raise ValueError("Host Python input lock changed or exceeds its budget")
    document=_json(raw)
    version=plan["target"]["python_version"]
    try:
        if (document["schema"] not in ("agentstrata-windows-python-inputs/v1", "agentstrata-windows-python-inputs/v2")
                or document["archives"]["host_python"]["version"]!=version
                or document["archives"]["host_python"]["selector"]!="cpython-"+version+"-windows-x86_64-none"):
            raise ValueError("Host Python input lock targets another runtime")
    except (KeyError,TypeError):
        raise ValueError("Host Python input lock has no matching runtime target") from None
    return canonical({"schema":"agentstrata-windows-python-environment-inputs/v3", "recipe":"windows-isolated-venv/v2",
        "python_inputs_sha256":expected,"lane":"host_python","requirements":plan["requirements"],
        "wheels":[{key:row[key] for key in ("name","version","sha256")} for row in plan["wheels"]]})
