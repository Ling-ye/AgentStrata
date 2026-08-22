#!/usr/bin/env python3
"""Verify AgentStrata release archives and their installed runtime resources."""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import os
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from collections import Counter
from dataclasses import dataclass
from email.message import Message
from email.parser import BytesParser
from email.policy import default as email_policy
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

try:
    import tomllib  # type: ignore[attr-defined]
except ModuleNotFoundError:  # Python 3.10
    from pip._vendor import tomli as tomllib  # type: ignore[no-redef]

from pip._vendor.packaging.markers import Marker
from pip._vendor.packaging.requirements import InvalidRequirement, Requirement
from pip._vendor.packaging.specifiers import InvalidSpecifier, SpecifierSet


ROOT = Path(__file__).resolve().parents[1]
_MARKDOWN_INLINE_TARGET_RE = re.compile(
    r"\]\(\s*(?P<target><[^>\r\n]*>|[^\s)\r\n]*)"
)
_MARKDOWN_REFERENCE_TARGET_RE = re.compile(
    r"(?m)^[ \t]{0,3}\[[^\]\r\n]+\]:[ \t]*"
    r"(?P<target><[^>\r\n]*>|[^\s\r\n]+)"
)
REQUIRED_PACKAGE_RESOURCES = frozenset(
    {
        "botspec/mcp_catalog.yaml",
        "evals/suites/agent-comparison/cases.yaml",
        "evals/suites/agentstrata-canary-self-update-v1/manifest.yaml",
        "evals/suites/agentstrata-capabilities-v1/README.md",
        "evals/suites/agentstrata-capabilities-v1/cases.yaml",
        "evals/suites/agentstrata-capabilities-v1/fixtures/order-card.png",
        "evals/suites/agentstrata-capabilities-v1/fixtures/sequence-first.png",
        "evals/suites/agentstrata-capabilities-v1/fixtures/sequence-second.png",
        "evals/suites/agentstrata-capabilities-v1/fixtures/shape-layout.png",
        "evals/suites/agentstrata-capabilities-v1/fixtures/untrusted-instructions.txt",
        "evals/suites/agentstrata-capabilities-v1/fixtures/workspace-note.txt",
        "evals/suites/agentstrata-capabilities-v1/manifest.yaml",
        "evals/suites/agentstrata-qq-message-flow-v1/README.md",
        "evals/suites/agentstrata-qq-message-flow-v1/cases.yaml",
        "evals/suites/agentstrata-qq-message-flow-v1/manifest.yaml",
        "evals/suites/bfcl/manifest.yaml",
        "evals/suites/gaia/manifest.yaml",
        "evals/suites/ifeval/manifest.yaml",
        "evals/suites/profiles.yaml",
        "evals/suites/profiles/agent-comparison-mvp/cases.yaml",
        "evals/suites/swe-bench-verified/manifest.yaml",
        "evals/suites/webarena/manifest.yaml",
        "external_tools/unity_codebase/projects.yaml",
        "external_tools/windows_fs/allowlist.yaml",
    }
)
_ENV_OVERRIDES = (
    "CHATCOPILOT_UNITY_PROJECTS",
    "CHATCOPILOT_UNITY_SAMPLE_GAME_ROOT",
    "CHATCOPILOT_WINDOWS_FS_ALLOWLIST",
    "CHATCOPILOT_WINDOWS_FS_EXTRA_ROOTS",
)
SDIST_GENERATED_FILES = frozenset(
    {
        "PKG-INFO",
        "setup.cfg",
        "src/agentstrata.egg-info/PKG-INFO",
        "src/agentstrata.egg-info/SOURCES.txt",
        "src/agentstrata.egg-info/dependency_links.txt",
        "src/agentstrata.egg-info/entry_points.txt",
        "src/agentstrata.egg-info/requires.txt",
        "src/agentstrata.egg-info/top_level.txt",
    }
)
SDIST_SOURCE_FILES = frozenset({"LICENSE", "README.md", "pyproject.toml"})
_SELF_CONTAINED_PROBE = r"""
import json
import site
import sys
import xml.etree.ElementTree as ElementTree
from pathlib import Path

expected_venv = Path(sys.argv[1]).resolve()
assert Path(sys.prefix).resolve() == expected_venv
assert sys.prefix != sys.base_prefix
assert site.ENABLE_USER_SITE is not True

import chatcopilot

package_init = Path(chatcopilot.__file__).resolve()
try:
    package_init.relative_to(expected_venv)
except ValueError as exc:
    raise AssertionError(
        f"chatcopilot imported outside isolated virtual environment: {package_init}"
    ) from exc

from chatcopilot.__main__ import main
assert main(["--help"]) == 0

package_root = package_init.parent
resource_paths = json.loads(sys.argv[2])
assert isinstance(resource_paths, list)
for relative in resource_paths:
    assert isinstance(relative, str) and relative
    resource = package_root / relative
    assert resource.is_file() and resource.stat().st_size > 0, relative
    payload = resource.read_bytes()
    if resource.suffix == ".json":
        json.loads(payload.decode("utf-8"))
    elif resource.suffix == ".xml":
        ElementTree.parse(resource)
    elif resource.suffix in {".md", ".txt", ".yaml", ".yml"}:
        assert payload.decode("utf-8").strip()

print(f"self-contained wheel import verified: {package_init}")
"""
_RUNTIME_PROBE = r"""
import json
import sys
import xml.etree.ElementTree as ElementTree
from pathlib import Path

target = Path(sys.argv[1]).resolve()
import chatcopilot

package_init = Path(chatcopilot.__file__).resolve()
try:
    package_init.relative_to(target)
except ValueError as exc:
    raise AssertionError(f"chatcopilot imported outside isolated target: {package_init}") from exc

from chatcopilot.__main__ import main
assert main(["--help"]) == 0

from chatcopilot.agent.context.prompt_plan import (
    PromptBuildInput,
    PromptPlanBuilder,
    render_native_prefix,
)
from chatcopilot.contracts.prompt import BotPromptProfile

plan = PromptPlanBuilder().build(
    PromptBuildInput(
        profile=BotPromptProfile(
            identity="Release probe assistant",
            response_style="Respond concisely.",
        ),
        backend="native",
        model=None,
        role="owner",
        channel_kind="private",
        session_policy="Exercise the installed PromptPlan runtime.",
    )
)
prompt_prefix = render_native_prefix(plan)
assert prompt_prefix[0]["role"] == "system"
assert any("Release probe assistant" in message["content"] for message in prompt_prefix)

from chatcopilot.external_tools.unity_codebase.config import load_registry
assert load_registry(force_reload=True).ids()

from chatcopilot.external_tools.windows_fs.config import load_config
windows_config = load_config(force_reload=True)
assert windows_config.allowed_roots == ()
assert windows_config.denied_patterns

from chatcopilot.evals.suite_loader import load_suite_cases
assert load_suite_cases("agent-comparison")

print(f"isolated import verified: {package_init}")
"""


class VerificationError(RuntimeError):
    """A release archive or installed probe violated the release contract."""


@dataclass(frozen=True)
class ArtifactIdentity:
    name: str
    version: str


def _canonical_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _identity(name: str, version: str, *, label: str) -> ArtifactIdentity:
    normalized_name = _canonical_name(name.strip())
    normalized_version = version.strip()
    if normalized_name != "agentstrata":
        raise VerificationError(f"{label} project name must be agentstrata, found {name!r}")
    if not normalized_version:
        raise VerificationError(f"{label} project version is empty")
    return ArtifactIdentity(name=normalized_name, version=normalized_version)


def _project_identity(data: dict[str, Any], *, label: str) -> ArtifactIdentity:
    project = data.get("project")
    if not isinstance(project, dict):
        raise VerificationError(f"{label} does not contain a project table")
    return _identity(
        str(project.get("name") or ""),
        str(project.get("version") or ""),
        label=label,
    )


def _source_project_data() -> dict[str, Any]:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise VerificationError("source pyproject.toml must contain a table")
    return data


def _source_identity() -> ArtifactIdentity:
    return _project_identity(_source_project_data(), label="source pyproject.toml")


def _parse_requires_python(value: str, *, label: str) -> SpecifierSet:
    try:
        return SpecifierSet(value)
    except InvalidSpecifier as exc:
        raise VerificationError(
            f"{label} contains invalid Requires-Python {value!r}"
        ) from exc


def _project_requires_python(data: dict[str, Any], *, label: str) -> SpecifierSet:
    project = data.get("project")
    if not isinstance(project, dict):
        raise VerificationError(f"{label} does not contain a project table")
    value = project.get("requires-python")
    if not isinstance(value, str) or not value.strip():
        raise VerificationError(f"{label} project.requires-python must be non-empty")
    return _parse_requires_python(value, label=label)


def _validate_requires_python_metadata(metadata: Message, *, label: str) -> None:
    expected = _project_requires_python(
        _source_project_data(),
        label="source pyproject.toml",
    )
    values = tuple(
        str(value).strip() for value in metadata.get_all("Requires-Python", [])
    )
    if len(values) != 1 or not values[0]:
        raise VerificationError(f"{label} must contain exactly one Requires-Python")
    actual = _parse_requires_python(values[0], label=label)
    if actual != expected:
        raise VerificationError(
            f"{label} Requires-Python differs from pyproject.toml: "
            f"expected={expected!s}, actual={actual!s}"
        )


RequirementKey = tuple[str, tuple[str, ...], str, str, str]


def _parse_requirement(value: str, *, label: str) -> Requirement:
    try:
        return Requirement(value)
    except InvalidRequirement as exc:
        raise VerificationError(f"{label} contains invalid requirement {value!r}") from exc


def _requirement_base(requirement: Requirement) -> str:
    result = requirement.name
    if requirement.extras:
        result += "[" + ",".join(sorted(requirement.extras)) + "]"
    if requirement.url:
        return f"{result} @ {requirement.url}"
    return result + str(requirement.specifier)


def _requirement_for_extra(value: str, *, extra: str, label: str) -> str:
    requirement = _parse_requirement(value, label=label)
    extra_marker = f'extra == "{_canonical_name(extra)}"'
    if requirement.marker is None:
        marker = Marker(extra_marker)
    else:
        marker = Marker(f"({requirement.marker}) and {extra_marker}")
    return f"{_requirement_base(requirement)}; {marker}"


def _project_dependency_values(
    data: dict[str, Any],
    *,
    label: str,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    project = data.get("project")
    if not isinstance(project, dict):
        raise VerificationError(f"{label} does not contain a project table")

    dependencies = project.get("dependencies", [])
    if not isinstance(dependencies, list) or not all(
        isinstance(value, str) for value in dependencies
    ):
        raise VerificationError(f"{label} project.dependencies must be a string list")

    optional = project.get("optional-dependencies", {})
    if not isinstance(optional, dict):
        raise VerificationError(f"{label} project.optional-dependencies must be a table")

    values = list(dependencies)
    extras: list[str] = []
    for extra, requirements in optional.items():
        if not isinstance(extra, str) or not extra:
            raise VerificationError(f"{label} contains an invalid optional dependency name")
        if not isinstance(requirements, list) or not all(
            isinstance(value, str) for value in requirements
        ):
            raise VerificationError(
                f"{label} optional dependency {extra!r} must be a string list"
            )
        canonical_extra = _canonical_name(extra)
        extras.append(canonical_extra)
        values.extend(
            _requirement_for_extra(
                value,
                extra=canonical_extra,
                label=f"{label} optional dependency {extra!r}",
            )
            for value in requirements
        )
    return tuple(values), tuple(extras)


def _requirement_key(value: str, *, label: str) -> RequirementKey:
    requirement = _parse_requirement(value, label=label)
    return (
        _canonical_name(requirement.name),
        tuple(sorted(_canonical_name(extra) for extra in requirement.extras)),
        str(requirement.specifier),
        requirement.url or "",
        str(requirement.marker or ""),
    )


def _dependency_contract(
    values: Iterable[str],
    extras: Iterable[str],
    *,
    label: str,
) -> tuple[Counter[RequirementKey], Counter[str]]:
    requirements = Counter(
        _requirement_key(value, label=label) for value in values
    )
    provided_extras = Counter(_canonical_name(extra) for extra in extras)
    return requirements, provided_extras


def _counter_difference(
    expected: Counter[Any],
    actual: Counter[Any],
) -> str:
    missing = expected - actual
    unexpected = actual - expected
    details: list[str] = []
    if missing:
        details.append("missing=" + repr(sorted(missing.items(), key=lambda item: repr(item[0]))))
    if unexpected:
        details.append(
            "unexpected=" + repr(sorted(unexpected.items(), key=lambda item: repr(item[0])))
        )
    return "; ".join(details)


def _markdown_targets(markdown: str) -> Iterable[str]:
    for pattern in (_MARKDOWN_INLINE_TARGET_RE, _MARKDOWN_REFERENCE_TARGET_RE):
        for match in pattern.finditer(markdown):
            target = match.group("target").strip()
            if target.startswith("<") and target.endswith(">"):
                target = target[1:-1].strip()
            yield target


def _validate_markdown_targets(markdown: str, *, label: str) -> None:
    for target in _markdown_targets(markdown):
        lowered = target.lower()
        if (
            target.startswith("#")
            or lowered.startswith("https://")
            or lowered.startswith("http://")
            or lowered.startswith("mailto:")
        ):
            continue
        raise VerificationError(
            f"{label} contains relative Markdown target {target!r}"
        )


def _validate_description_metadata(metadata: Message, *, label: str) -> None:
    payload = metadata.get_payload()
    if isinstance(payload, list):
        raise VerificationError(f"{label} Description must not be multipart")
    _validate_markdown_targets(str(payload or ""), label=f"{label} Description")


def _validate_dependency_metadata(metadata: Message, *, label: str) -> None:
    expected_values, expected_extras = _project_dependency_values(
        _source_project_data(),
        label="source pyproject.toml",
    )
    expected_requirements, expected_provided_extras = _dependency_contract(
        expected_values,
        expected_extras,
        label="source pyproject.toml",
    )
    actual_values = tuple(str(value) for value in metadata.get_all("Requires-Dist", []))
    actual_extras = tuple(str(value) for value in metadata.get_all("Provides-Extra", []))
    actual_requirements, actual_provided_extras = _dependency_contract(
        actual_values,
        actual_extras,
        label=label,
    )
    if actual_requirements != expected_requirements:
        raise VerificationError(
            f"{label} Requires-Dist differs from pyproject.toml: "
            + _counter_difference(expected_requirements, actual_requirements)
        )
    if actual_provided_extras != expected_provided_extras:
        raise VerificationError(
            f"{label} Provides-Extra differs from pyproject.toml: "
            + _counter_difference(expected_provided_extras, actual_provided_extras)
        )


def _validate_core_metadata(metadata: Message, *, label: str) -> None:
    _validate_requires_python_metadata(metadata, label=label)
    _validate_dependency_metadata(metadata, label=label)
    _validate_description_metadata(metadata, label=label)
    _validate_markdown_targets(
        (ROOT / "README.md").read_text(encoding="utf-8"),
        label="source README.md",
    )


def _wheel_filename_identity(wheel: Path) -> ArtifactIdentity:
    if not wheel.name.endswith(".whl"):
        raise VerificationError(f"wheel must end with .whl: {wheel.name}")
    parts = wheel.name[: -len(".whl")].split("-")
    if len(parts) < 5:
        raise VerificationError(f"invalid wheel filename: {wheel.name}")
    return _identity(parts[0], parts[1], label="wheel filename")


def _validate_member_path(name: str, *, archive_label: str) -> None:
    path = PurePosixPath(name)
    if (
        not name
        or "\\" in name
        or "//" in name
        or name.startswith("./")
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise VerificationError(f"{archive_label} contains unsafe member path: {name!r}")


def _reject_duplicate_members(names: Iterable[str], *, archive_label: str) -> None:
    duplicates = sorted(name for name, count in Counter(names).items() if count > 1)
    if duplicates:
        raise VerificationError(
            f"{archive_label} contains duplicate members: {', '.join(duplicates)}"
        )


def _validate_zip_member(info: zipfile.ZipInfo) -> None:
    mode = info.external_attr >> 16
    file_type = stat.S_IFMT(mode)
    if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
        raise VerificationError(
            f"wheel contains non-regular member: {info.filename!r}"
        )
    if file_type == stat.S_IFDIR and not info.is_dir():
        raise VerificationError(f"wheel member type disagrees with path: {info.filename!r}")
    if file_type == stat.S_IFREG and info.is_dir():
        raise VerificationError(f"wheel member type disagrees with path: {info.filename!r}")


def _parse_wheel_record(
    payload: bytes,
    *,
    record_path: str,
) -> dict[str, tuple[str, str]]:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise VerificationError("wheel RECORD is not valid UTF-8") from exc

    entries: dict[str, tuple[str, str]] = {}
    try:
        reader = csv.reader(io.StringIO(text, newline=""))
        for row in reader:
            if len(row) != 3:
                raise VerificationError(
                    f"wheel RECORD row {reader.line_num} must contain exactly three fields"
                )
            path, hash_value, size_value = row
            _validate_member_path(path, archive_label="wheel RECORD")
            if path in entries:
                raise VerificationError(f"wheel RECORD contains duplicate entry: {path}")
            entries[path] = (hash_value, size_value)
    except csv.Error as exc:
        raise VerificationError("wheel RECORD is not valid CSV") from exc

    if record_path not in entries:
        raise VerificationError("wheel RECORD does not list itself")
    return entries


def _record_hash(payload: bytes) -> str:
    digest = hashlib.sha256(payload).digest()
    encoded = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return f"sha256={encoded}"


def _validate_wheel_record(
    archive: zipfile.ZipFile,
    *,
    file_names: frozenset[str],
    record_path: str,
) -> dict[str, tuple[str, str]]:
    entries = _parse_wheel_record(archive.read(record_path), record_path=record_path)
    recorded_names = frozenset(entries)
    unrecorded = sorted(file_names - recorded_names)
    missing_members = sorted(recorded_names - file_names)
    if unrecorded or missing_members:
        details: list[str] = []
        if unrecorded:
            details.append("unrecorded=" + ", ".join(unrecorded))
        if missing_members:
            details.append("missing-members=" + ", ".join(missing_members))
        raise VerificationError(f"wheel RECORD coverage differs: {'; '.join(details)}")

    for path, (hash_value, size_value) in entries.items():
        if path == record_path:
            if hash_value or size_value:
                raise VerificationError("wheel RECORD self-entry must omit hash and size")
            continue
        if not hash_value or not size_value:
            raise VerificationError(
                f"wheel RECORD entry must include hash and size: {path}"
            )
        payload = archive.read(path)
        if size_value != str(len(payload)):
            raise VerificationError(f"wheel RECORD size mismatch: {path}")
        if hash_value != _record_hash(payload):
            raise VerificationError(f"wheel RECORD hash mismatch: {path}")
    return entries


def _canonical_record_payload(entries: dict[str, tuple[str, str]]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    for path in sorted(entries):
        hash_value, size_value = entries[path]
        writer.writerow((path, hash_value, size_value))
    return output.getvalue().encode("utf-8")


def _canonical_wheel_contents(wheel: Path) -> dict[str, str]:
    with zipfile.ZipFile(wheel) as archive:
        file_names = frozenset(
            info.filename for info in archive.infolist() if not info.is_dir()
        )
        record_paths = sorted(
            name for name in file_names if name.endswith(".dist-info/RECORD")
        )
        if len(record_paths) != 1:
            raise VerificationError(
                f"wheel must contain one RECORD file, found {len(record_paths)}"
            )
        record_path = record_paths[0]
        entries = _validate_wheel_record(
            archive,
            file_names=file_names,
            record_path=record_path,
        )
        contents: dict[str, str] = {}
        for name in file_names:
            payload = (
                _canonical_record_payload(entries)
                if name == record_path
                else archive.read(name)
            )
            contents[name] = hashlib.sha256(payload).hexdigest()
        return contents


def _compare_wheel_contents(built: Path, rebuilt: Path) -> None:
    built_contents = _canonical_wheel_contents(built)
    rebuilt_contents = _canonical_wheel_contents(rebuilt)
    built_names = set(built_contents)
    rebuilt_names = set(rebuilt_contents)
    only_in_built = sorted(built_names - rebuilt_names)
    only_in_rebuilt = sorted(rebuilt_names - built_names)
    changed = sorted(
        name
        for name in built_names & rebuilt_names
        if built_contents[name] != rebuilt_contents[name]
    )
    if not only_in_built and not only_in_rebuilt and not changed:
        return
    details: list[str] = []
    if only_in_built:
        details.append("only-in-built=" + ", ".join(only_in_built))
    if only_in_rebuilt:
        details.append("only-in-rebuilt=" + ", ".join(only_in_rebuilt))
    if changed:
        details.append("content-changed=" + ", ".join(changed))
    raise VerificationError(
        f"built and sdist-rebuilt wheel contents differ: {'; '.join(details)}"
    )


def _package_resources(file_names: Iterable[str], *, prefix: str) -> frozenset[str]:
    resources: set[str] = set()
    for name in file_names:
        if not name.startswith(prefix):
            continue
        relative = name[len(prefix) :]
        if relative.endswith((".py", ".pyi")):
            continue
        resources.add(relative)
    return frozenset(resources)


def _assert_exact_resources(resources: frozenset[str], *, archive_label: str) -> None:
    missing = sorted(REQUIRED_PACKAGE_RESOURCES - resources)
    unexpected = sorted(resources - REQUIRED_PACKAGE_RESOURCES)
    if not missing and not unexpected:
        return
    details: list[str] = []
    if missing:
        details.append("missing=" + ", ".join(missing))
    if unexpected:
        details.append("unexpected=" + ", ".join(unexpected))
    raise VerificationError(f"{archive_label} package resources differ: {'; '.join(details)}")


def _source_package_projection() -> frozenset[str]:
    package_root = ROOT / "src" / "chatcopilot"
    completed = subprocess.run(
        ("git", "ls-files", "-z", "--", "src/chatcopilot"),
        cwd=ROOT,
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        raise VerificationError("cannot read reviewed package projection from git")
    python_members: set[str] = set()
    for raw_path in completed.stdout.split(b"\0"):
        if not raw_path:
            continue
        try:
            repo_relative = Path(raw_path.decode("utf-8"))
            relative = repo_relative.relative_to("src/chatcopilot")
        except (UnicodeDecodeError, ValueError) as exc:
            raise VerificationError("git returned an invalid package path") from exc
        path = ROOT / repo_relative
        if path.suffix not in {".py", ".pyi"}:
            continue
        if not path.is_file():
            raise VerificationError(f"tracked package source is missing: {repo_relative}")
        if path.is_symlink():
            raise VerificationError(f"source package contains symlink: {path}")
        python_members.add(relative.as_posix())
    for relative in REQUIRED_PACKAGE_RESOURCES:
        resource = package_root / relative
        if not resource.is_file() or resource.is_symlink():
            raise VerificationError(
                f"source package resource must be a regular file: {relative}"
            )
    return frozenset(python_members | set(REQUIRED_PACKAGE_RESOURCES))


def _expected_sdist_files(expected_root: str) -> frozenset[str]:
    relative_files = (
        set(SDIST_SOURCE_FILES)
        | set(SDIST_GENERATED_FILES)
        | {
            f"src/chatcopilot/{relative}"
            for relative in _source_package_projection()
        }
    )
    return frozenset(f"{expected_root}/{relative}" for relative in relative_files)


def _assert_exact_sdist_files(
    file_names: frozenset[str],
    *,
    expected_root: str,
) -> None:
    expected = _expected_sdist_files(expected_root)
    missing = sorted(expected - file_names)
    unexpected = sorted(file_names - expected)
    if not missing and not unexpected:
        return
    details: list[str] = []
    if missing:
        details.append("missing=" + ", ".join(missing))
    if unexpected:
        details.append("unexpected=" + ", ".join(unexpected))
    raise VerificationError(f"sdist file projection differs: {'; '.join(details)}")


def _read_sdist_file(archive: tarfile.TarFile, name: str) -> bytes:
    member = archive.extractfile(name)
    if member is None:
        raise VerificationError(f"sdist member is not a regular file: {name}")
    return member.read()


def _validate_sdist_source_payloads(
    archive: tarfile.TarFile,
    *,
    expected_root: str,
) -> None:
    source_files = set(SDIST_SOURCE_FILES) | {
        f"src/chatcopilot/{relative}" for relative in _source_package_projection()
    }
    for relative in sorted(source_files):
        source = ROOT / relative
        archived = _read_sdist_file(archive, f"{expected_root}/{relative}")
        if archived != source.read_bytes():
            raise VerificationError(f"sdist source payload differs: {relative}")


def validate_wheel(wheel: Path) -> ArtifactIdentity:
    wheel = wheel.resolve(strict=True)
    filename_identity = _wheel_filename_identity(wheel)
    try:
        with zipfile.ZipFile(wheel) as archive:
            broken = archive.testzip()
            if broken is not None:
                raise VerificationError(f"wheel contains corrupt member: {broken}")
            infos = tuple(archive.infolist())
            normalized_names = tuple(info.filename.rstrip("/") for info in infos)
            _reject_duplicate_members(normalized_names, archive_label="wheel")
            for info, normalized_name in zip(infos, normalized_names, strict=True):
                _validate_member_path(normalized_name, archive_label="wheel")
                _validate_zip_member(info)
            file_names = frozenset(info.filename for info in infos if not info.is_dir())
            _assert_exact_resources(
                _package_resources(file_names, prefix="chatcopilot/"),
                archive_label="wheel",
            )
            dist_infos = {
                PurePosixPath(name).parts[0]
                for name in file_names
                if PurePosixPath(name).parts[0].endswith(".dist-info")
            }
            if len(dist_infos) != 1:
                raise VerificationError(
                    f"wheel must contain one dist-info directory, found {len(dist_infos)}"
                )
            dist_info = next(iter(dist_infos))
            required_metadata = {
                f"{dist_info}/METADATA",
                f"{dist_info}/RECORD",
                f"{dist_info}/WHEEL",
                f"{dist_info}/entry_points.txt",
                f"{dist_info}/licenses/LICENSE",
            }
            missing_metadata = sorted(required_metadata - file_names)
            if missing_metadata:
                raise VerificationError(
                    "wheel missing metadata: " + ", ".join(missing_metadata)
                )
            _validate_wheel_record(
                archive,
                file_names=file_names,
                record_path=f"{dist_info}/RECORD",
            )
            entry_points = archive.read(f"{dist_info}/entry_points.txt").decode("utf-8")
            if "agentstrata = chatcopilot.__main__:main" not in entry_points:
                raise VerificationError("wheel does not expose the agentstrata CLI entry point")
            metadata = BytesParser(policy=email_policy).parsebytes(
                archive.read(f"{dist_info}/METADATA")
            )
            metadata_identity = _identity(
                str(metadata.get("Name") or ""),
                str(metadata.get("Version") or ""),
                label="wheel METADATA",
            )
            if filename_identity != metadata_identity:
                raise VerificationError(
                    "wheel filename identity does not match METADATA: "
                    f"{filename_identity!r} != {metadata_identity!r}"
                )
            _validate_core_metadata(metadata, label="wheel METADATA")
            return metadata_identity
    except zipfile.BadZipFile as exc:
        raise VerificationError(f"invalid wheel archive: {wheel.name}") from exc


def _sdist_stem(sdist: Path) -> str:
    if not sdist.name.endswith(".tar.gz"):
        raise VerificationError(f"sdist must end with .tar.gz: {sdist.name}")
    return sdist.name[: -len(".tar.gz")]


def validate_sdist(sdist: Path) -> ArtifactIdentity:
    sdist = sdist.resolve(strict=True)
    expected_root = _sdist_stem(sdist)
    try:
        with tarfile.open(sdist, mode="r:gz") as archive:
            members = tuple(archive.getmembers())
            normalized_names = tuple(member.name.rstrip("/") for member in members)
            _reject_duplicate_members(normalized_names, archive_label="sdist")
            for member, normalized_name in zip(members, normalized_names, strict=True):
                _validate_member_path(normalized_name, archive_label="sdist")
                if not (member.isfile() or member.isdir()):
                    raise VerificationError(
                        f"sdist contains non-regular member: {member.name!r}"
                    )
            roots = {PurePosixPath(member.name).parts[0] for member in members}
            if roots != {expected_root}:
                raise VerificationError(
                    f"sdist root must be {expected_root!r}, found {sorted(roots)!r}"
                )
            file_names = frozenset(member.name for member in members if member.isfile())
            _assert_exact_sdist_files(file_names, expected_root=expected_root)
            _assert_exact_resources(
                _package_resources(
                    file_names,
                    prefix=f"{expected_root}/src/chatcopilot/",
                ),
                archive_label="sdist",
            )
            _validate_sdist_source_payloads(archive, expected_root=expected_root)
            identity = _project_identity(
                tomllib.loads(
                    _read_sdist_file(
                        archive,
                        f"{expected_root}/pyproject.toml",
                    ).decode("utf-8")
                ),
                label="sdist pyproject.toml",
            )
            identity_root = f"{identity.name}-{identity.version}"
            if expected_root != identity_root:
                raise VerificationError(
                    f"sdist filename identity must be {identity_root!r}, found {expected_root!r}"
                )
            for relative in (
                "PKG-INFO",
                "src/agentstrata.egg-info/PKG-INFO",
            ):
                metadata_label = f"sdist {relative}"
                metadata = BytesParser(policy=email_policy).parsebytes(
                    _read_sdist_file(archive, f"{expected_root}/{relative}")
                )
                metadata_identity = _identity(
                    str(metadata.get("Name") or ""),
                    str(metadata.get("Version") or ""),
                    label=metadata_label,
                )
                if metadata_identity != identity:
                    raise VerificationError(
                        f"{metadata_label} identity does not match pyproject.toml"
                    )
                _validate_core_metadata(metadata, label=metadata_label)
            return identity
    except tarfile.TarError as exc:
        raise VerificationError(f"invalid sdist archive: {sdist.name}") from exc


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _run_checked(
    argv: tuple[str, ...],
    *,
    cwd: Path,
    label: str,
    env: dict[str, str] | None = None,
) -> None:
    completed = subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode == 0:
        return
    detail = "\n".join(
        part.strip() for part in (completed.stdout, completed.stderr) if part.strip()
    )
    if detail:
        detail = "\n" + detail[-4000:]
    raise VerificationError(f"{label} failed with exit code {completed.returncode}{detail}")


def _venv_python(venv_root: Path) -> Path:
    if os.name == "nt":
        return venv_root / "Scripts" / "python.exe"
    return venv_root / "bin" / "python"


def install_and_probe_wheel(
    wheel: Path,
    *,
    label: str,
    install_dependencies: bool = False,
    wheelhouse: Path | None = None,
) -> None:
    wheel = wheel.resolve(strict=True)
    if wheelhouse is not None:
        wheelhouse = wheelhouse.resolve(strict=True)
        if not wheelhouse.is_dir():
            raise VerificationError(f"wheelhouse is not a directory: {wheelhouse}")
    if wheelhouse is not None and not install_dependencies:
        raise VerificationError("wheelhouse requires dependency installation")

    with tempfile.TemporaryDirectory(prefix="agentstrata-wheel-probe-") as temp_dir:
        temp_root = Path(temp_dir).resolve()
        outside_cwd = temp_root / "cwd"
        venv_root = temp_root / "venv"
        outside_cwd.mkdir()
        if _is_within(outside_cwd, ROOT):
            raise VerificationError("isolated probe cwd resolved inside the repository")

        env = os.environ.copy()
        for name in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"):
            env.pop(name, None)
        env["PYTHONNOUSERSITE"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        for name in _ENV_OVERRIDES:
            env.pop(name, None)

        _run_checked(
            (sys.executable, "-m", "venv", str(venv_root)),
            cwd=outside_cwd,
            env=env,
            label=f"{label} virtual environment creation",
        )
        venv_config = (venv_root / "pyvenv.cfg").read_text(encoding="utf-8").lower()
        if "include-system-site-packages = false" not in venv_config:
            raise VerificationError(
                f"{label} probe virtual environment exposes system site-packages"
            )
        python = _venv_python(venv_root)
        if not python.is_file():
            raise VerificationError(f"{label} virtual environment has no Python executable")

        install_argv = [
            str(python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-cache-dir",
        ]
        if install_dependencies:
            if wheelhouse is not None:
                install_argv.extend(("--no-index", "--find-links", str(wheelhouse)))
        else:
            install_argv.extend(("--no-index", "--no-deps"))
        install_argv.extend(("--no-compile", str(wheel)))
        _run_checked(
            tuple(install_argv),
            cwd=outside_cwd,
            env=env,
            label=f"{label} installation",
        )
        probe = _RUNTIME_PROBE if install_dependencies else _SELF_CONTAINED_PROBE
        probe_kind = "normal-install runtime" if install_dependencies else "no-deps self-contained"
        _run_checked(
            (
                str(python),
                "-c",
                probe,
                str(venv_root),
                json.dumps(sorted(REQUIRED_PACKAGE_RESOURCES), separators=(",", ":")),
            ),
            cwd=outside_cwd,
            env=env,
            label=f"{label} {probe_kind} probe",
        )


def build_wheel_from_sdist(sdist: Path, *, output_dir: Path) -> Path:
    sdist = sdist.resolve(strict=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    _run_checked(
        (
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--disable-pip-version-check",
            "--no-cache-dir",
            "--no-index",
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            str(output_dir),
            str(sdist),
        ),
        cwd=output_dir,
        label="wheel rebuild from sdist",
    )
    wheels = tuple(output_dir.glob("agentstrata-*.whl"))
    if len(wheels) != 1:
        raise VerificationError(
            f"sdist rebuild produced {len(wheels)} AgentStrata wheels"
        )
    return wheels[0]


def verify_release_artifacts(
    *,
    wheel: Path,
    sdist: Path,
    normal_install: bool = False,
    wheelhouse: Path | None = None,
) -> None:
    wheel = wheel.resolve(strict=True)
    sdist = sdist.resolve(strict=True)
    source_identity = _source_identity()
    wheel_identity = validate_wheel(wheel)
    sdist_identity = validate_sdist(sdist)
    if wheel_identity != source_identity or sdist_identity != source_identity:
        raise VerificationError(
            "artifact identity does not match source pyproject.toml: "
            f"source={source_identity!r}, wheel={wheel_identity!r}, sdist={sdist_identity!r}"
        )
    install_and_probe_wheel(wheel, label="built wheel")
    if normal_install:
        install_and_probe_wheel(
            wheel,
            label="built wheel",
            install_dependencies=True,
            wheelhouse=wheelhouse,
        )

    with tempfile.TemporaryDirectory(prefix="agentstrata-sdist-rebuild-") as temp_dir:
        rebuilt = build_wheel_from_sdist(sdist, output_dir=Path(temp_dir))
        if rebuilt.name != wheel.name:
            raise VerificationError(
                f"sdist rebuilt {rebuilt.name}, expected {wheel.name}"
            )
        rebuilt_identity = validate_wheel(rebuilt)
        if rebuilt_identity != source_identity:
            raise VerificationError(
                "sdist-rebuilt wheel identity does not match source pyproject.toml"
            )
        _compare_wheel_contents(wheel, rebuilt)
        install_and_probe_wheel(rebuilt, label="sdist-rebuilt wheel")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify exact release members and isolated wheel runtime behavior."
    )
    parser.add_argument("--wheel", required=True, type=Path)
    parser.add_argument("--sdist", required=True, type=Path)
    parser.add_argument(
        "--normal-install",
        action="store_true",
        help="also install declared dependencies in a fresh venv and run runtime probes",
    )
    parser.add_argument(
        "--wheelhouse",
        type=Path,
        help="resolve normal-install dependencies only from this local wheelhouse",
    )
    args = parser.parse_args(argv)
    if args.wheelhouse is not None and not args.normal_install:
        parser.error("--wheelhouse requires --normal-install")
    try:
        verify_release_artifacts(
            wheel=args.wheel,
            sdist=args.sdist,
            normal_install=args.normal_install,
            wheelhouse=args.wheelhouse,
        )
    except (OSError, VerificationError) as exc:
        print(f"release artifact verification failed: {exc}", file=sys.stderr)
        return 1
    print("release artifacts passed exact-member and isolated-runtime verification")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
