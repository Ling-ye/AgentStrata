from __future__ import annotations

import base64
import builtins
import csv
import hashlib
import importlib.util
import io
import stat
import sys
import tarfile
import types
import zipfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


def _worktree_package_projection(required_resources: frozenset[str]) -> frozenset[str]:
    package_root = ROOT / "src" / "chatcopilot"
    python_members = {
        path.relative_to(package_root).as_posix()
        for path in package_root.rglob("*")
        if path.suffix in {".py", ".pyi"} and path.is_file() and not path.is_symlink()
    }
    for relative in required_resources:
        resource = package_root / relative
        assert resource.is_file()
        assert not resource.is_symlink()
    return frozenset(python_members | set(required_resources))


def _load_verifier():
    path = ROOT / "scripts" / "verify_release_artifacts.py"
    spec = importlib.util.spec_from_file_location("verify_release_artifacts_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_release_verifier_python310_fallback_uses_public_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = ROOT / "scripts" / "verify_release_artifacts.py"
    assert "pip._vendor" not in path.read_text(encoding="utf-8")

    fake_tomli = types.ModuleType("tomli")
    fake_tomli.loads = lambda value: {"source": value}
    original_import = builtins.__import__

    def import_without_tomllib_or_pip(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "tomllib":
            raise ModuleNotFoundError(name)
        if name == "pip" or name.startswith("pip."):
            raise AssertionError("release verifier must not import pip")
        return original_import(name, globals, locals, fromlist, level)

    module_name = "verify_release_artifacts_test_python310"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "tomli", fake_tomli)
    monkeypatch.setitem(sys.modules, module_name, module)
    monkeypatch.setattr(builtins, "__import__", import_without_tomllib_or_pip)
    spec.loader.exec_module(module)

    assert module.tomllib is fake_tomli
    assert module.tomllib.loads("key = 'value'") == {"source": "key = 'value'"}
    assert module.Requirement.__module__ == "packaging.requirements"


def _load_synthetic_sdist_verifier():
    verifier = _load_verifier()
    projection = _worktree_package_projection(verifier.REQUIRED_PACKAGE_RESOURCES)
    verifier._source_package_projection = lambda: projection
    return verifier


def _record_hash(payload: bytes) -> str:
    digest = hashlib.sha256(payload).digest()
    encoded = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return f"sha256={encoded}"


def _metadata_payload(
    verifier,
    *,
    version: str = "1.0",
    description: str = "AgentStrata release.\n",
) -> bytes:
    identity = verifier._source_identity()
    source_project = verifier._source_project_data()["project"]
    requirements, extras = verifier._project_dependency_values(
        verifier._source_project_data(),
        label="test pyproject.toml",
    )
    lines = [
        "Metadata-Version: 2.4",
        f"Name: {identity.name}",
        f"Version: {version}",
        f"Requires-Python: {source_project['requires-python']}",
        *(f"Requires-Dist: {requirement}" for requirement in requirements),
        *(f"Provides-Extra: {extra}" for extra in extras),
    ]
    return ("\n".join(lines) + "\n\n" + description).encode("utf-8")


def _write_test_wheel(
    path: Path,
    verifier,
    *,
    member_overrides: dict[str, bytes] | None = None,
    extra_members: dict[str, bytes] | None = None,
    omitted_record_paths: frozenset[str] = frozenset(),
    duplicate_record_path: str | None = None,
    record_overrides: dict[str, tuple[str, str]] | None = None,
    metadata_payload: bytes | None = None,
    timestamp: tuple[int, int, int, int, int, int] = (2024, 1, 1, 0, 0, 0),
) -> None:
    dist_info = "agentstrata-1.0.dist-info"
    record_path = f"{dist_info}/RECORD"
    members: dict[str, bytes] = {
        "chatcopilot/__init__.py": b"",
        **{
            f"chatcopilot/{relative}": b"release resource\n"
            for relative in verifier.REQUIRED_PACKAGE_RESOURCES
        },
        f"{dist_info}/METADATA": metadata_payload or _metadata_payload(verifier),
        f"{dist_info}/WHEEL": b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        f"{dist_info}/entry_points.txt": (
            b"[console_scripts]\nagentstrata = chatcopilot.__main__:main\n"
        ),
        f"{dist_info}/licenses/LICENSE": b"MIT\n",
    }
    members.update(member_overrides or {})
    members.update(extra_members or {})
    overrides = record_overrides or {}
    rows: list[tuple[str, str, str]] = []
    for name, payload in sorted(members.items()):
        if name in omitted_record_paths:
            continue
        hash_value, size_value = overrides.get(
            name,
            (_record_hash(payload), str(len(payload))),
        )
        rows.append((name, hash_value, size_value))
    rows.append((record_path, "", ""))
    if duplicate_record_path is not None:
        duplicate = next(row for row in rows if row[0] == duplicate_record_path)
        rows.append(duplicate)

    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerows(rows)
    members[record_path] = output.getvalue().encode("utf-8")

    with zipfile.ZipFile(path, mode="w") as archive:
        for name, payload in members.items():
            info = zipfile.ZipInfo(name, date_time=timestamp)
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(info, payload)


def _write_test_sdist(
    path: Path,
    verifier,
    *,
    extra_members: dict[str, bytes],
    metadata_payload: bytes | None = None,
) -> None:
    identity = verifier._source_identity()
    expected_root = f"{identity.name}-{identity.version}"
    metadata = metadata_payload or _metadata_payload(
        verifier,
        version=identity.version,
    )
    generated_payloads = {
        "PKG-INFO": metadata,
        "setup.cfg": b"[egg_info]\ntag_build =\ntag_date = 0\n",
        "src/agentstrata.egg-info/PKG-INFO": metadata,
        "src/agentstrata.egg-info/SOURCES.txt": b"generated\n",
        "src/agentstrata.egg-info/dependency_links.txt": b"\n",
        "src/agentstrata.egg-info/entry_points.txt": (
            b"[console_scripts]\nagentstrata = chatcopilot.__main__:main\n"
        ),
        "src/agentstrata.egg-info/requires.txt": b"generated\n",
        "src/agentstrata.egg-info/top_level.txt": b"chatcopilot\n",
    }
    members: dict[str, bytes] = {}
    for name in verifier._expected_sdist_files(expected_root):
        relative = name.removeprefix(f"{expected_root}/")
        if relative in verifier.SDIST_SOURCE_FILES or relative.startswith(
            "src/chatcopilot/"
        ):
            payload = (ROOT / relative).read_bytes()
        else:
            payload = generated_payloads[relative]
        members[name] = payload
    members.update(extra_members)

    with tarfile.open(path, mode="w:gz") as archive:
        for name, payload in sorted(members.items()):
            info = tarfile.TarInfo(name)
            info.mode = 0o644
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))


def test_release_resource_allowlist_is_exact() -> None:
    verifier = _load_verifier()
    assert verifier.REQUIRED_PACKAGE_RESOURCES == frozenset(
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


def test_release_runtime_probe_uses_canonical_prompt_plan() -> None:
    verifier = _load_verifier()
    assert "builtin_prompts" not in verifier._RUNTIME_PROBE
    assert "PromptPlanBuilder" in verifier._RUNTIME_PROBE


def test_release_resource_allowlist_rejects_drift() -> None:
    verifier = _load_verifier()
    required = verifier.REQUIRED_PACKAGE_RESOURCES
    with pytest.raises(verifier.VerificationError, match="missing="):
        verifier._assert_exact_resources(
            required - {"botspec/mcp_catalog.yaml"},
            archive_label="test",
        )
    with pytest.raises(verifier.VerificationError, match="unexpected="):
        verifier._assert_exact_resources(
            required | {"agent/requirements.txt"},
            archive_label="test",
        )


@pytest.mark.parametrize(
    "member",
    ("../secret", "chatcopilot/../secret", "/absolute", "windows\\path"),
)
def test_release_member_paths_reject_traversal(member: str) -> None:
    verifier = _load_verifier()
    with pytest.raises(verifier.VerificationError, match="unsafe member path"):
        verifier._validate_member_path(member, archive_label="test")


def test_release_archives_reject_duplicate_members() -> None:
    verifier = _load_verifier()
    with pytest.raises(verifier.VerificationError, match="duplicate members"):
        verifier._reject_duplicate_members(
            ("chatcopilot/data.json", "chatcopilot/data.json"),
            archive_label="test",
        )


def test_release_wheel_rejects_unix_symlink(tmp_path: Path) -> None:
    verifier = _load_verifier()
    wheel = tmp_path / "agentstrata-1.0-py3-none-any.whl"
    info = zipfile.ZipInfo("chatcopilot/link")
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(wheel, mode="w") as archive:
        archive.writestr(info, "target")

    with pytest.raises(verifier.VerificationError, match="non-regular member"):
        verifier.validate_wheel(wheel)


def test_release_sdist_rejects_fifo(tmp_path: Path) -> None:
    verifier = _load_verifier()
    sdist = tmp_path / "agentstrata-1.0.tar.gz"
    fifo = tarfile.TarInfo("agentstrata-1.0/pipe")
    fifo.type = tarfile.FIFOTYPE
    with tarfile.open(sdist, mode="w:gz") as archive:
        archive.addfile(fifo)

    with pytest.raises(verifier.VerificationError, match="non-regular member"):
        verifier.validate_sdist(sdist)


def test_release_wheel_rejects_unrecorded_python_member(tmp_path: Path) -> None:
    verifier = _load_verifier()
    wheel = tmp_path / "agentstrata-1.0-py3-none-any.whl"
    malicious = "chatcopilot/redteam_extra.py"
    _write_test_wheel(
        wheel,
        verifier,
        extra_members={malicious: b"REDTEAM = True\n"},
        omitted_record_paths=frozenset({malicious}),
    )

    with pytest.raises(verifier.VerificationError, match="unrecorded=.*redteam_extra"):
        verifier.validate_wheel(wheel)


@pytest.mark.parametrize("difference", ("missing", "unexpected"))
def test_release_wheel_requires_dist_must_match_pyproject(
    tmp_path: Path,
    difference: str,
) -> None:
    verifier = _load_verifier()
    wheel = tmp_path / "agentstrata-1.0-py3-none-any.whl"
    metadata = _metadata_payload(verifier)
    requirements, _ = verifier._project_dependency_values(
        verifier._source_project_data(),
        label="test pyproject.toml",
    )
    if difference == "missing":
        line = f"Requires-Dist: {requirements[0]}\n".encode()
        assert line in metadata
        metadata = metadata.replace(line, b"", 1)
    else:
        metadata = metadata.replace(
            b"\n\n",
            b"\nRequires-Dist: redteam-package>=1\n\n",
            1,
        )
    _write_test_wheel(wheel, verifier, metadata_payload=metadata)

    with pytest.raises(
        verifier.VerificationError,
        match=rf"Requires-Dist differs.*{difference}=",
    ):
        verifier.validate_wheel(wheel)


@pytest.mark.parametrize(
    "relative",
    ("redteam.env", "src/chatcopilot/redteam_extra.py"),
)
def test_release_sdist_rejects_unexpected_regular_file(
    tmp_path: Path,
    relative: str,
) -> None:
    verifier = _load_synthetic_sdist_verifier()
    identity = verifier._source_identity()
    root = f"{identity.name}-{identity.version}"
    sdist = tmp_path / f"{root}.tar.gz"
    _write_test_sdist(
        sdist,
        verifier,
        extra_members={f"{root}/{relative}": b"redteam\n"},
    )

    with pytest.raises(
        verifier.VerificationError,
        match="sdist file projection differs.*unexpected=.*redteam",
    ):
        verifier.validate_sdist(sdist)


def test_release_wheel_rejects_duplicate_record_entry(tmp_path: Path) -> None:
    verifier = _load_verifier()
    wheel = tmp_path / "agentstrata-1.0-py3-none-any.whl"
    _write_test_wheel(
        wheel,
        verifier,
        duplicate_record_path="chatcopilot/__init__.py",
    )

    with pytest.raises(verifier.VerificationError, match="duplicate entry"):
        verifier.validate_wheel(wheel)


@pytest.mark.parametrize(
    ("record_override", "message"),
    (
        (("sha256=invalid", "0"), "size mismatch"),
        (("sha256=invalid", "1"), "hash mismatch"),
    ),
)
def test_release_wheel_rejects_record_hash_or_size_mismatch(
    tmp_path: Path,
    record_override: tuple[str, str],
    message: str,
) -> None:
    verifier = _load_verifier()
    wheel = tmp_path / "agentstrata-1.0-py3-none-any.whl"
    _write_test_wheel(
        wheel,
        verifier,
        member_overrides={"chatcopilot/__init__.py": b"x"},
        record_overrides={"chatcopilot/__init__.py": record_override},
    )

    with pytest.raises(verifier.VerificationError, match=message):
        verifier.validate_wheel(wheel)


@pytest.mark.parametrize(
    ("built_extra", "rebuilt_extra", "difference"),
    (
        ({"chatcopilot/redteam_extra.py": b"x"}, {}, "only-in-built"),
        ({}, {"chatcopilot/redteam_extra.py": b"x"}, "only-in-rebuilt"),
    ),
)
def test_release_wheel_comparison_rejects_python_member_drift(
    tmp_path: Path,
    built_extra: dict[str, bytes],
    rebuilt_extra: dict[str, bytes],
    difference: str,
) -> None:
    verifier = _load_verifier()
    built = tmp_path / "built" / "agentstrata-1.0-py3-none-any.whl"
    rebuilt = tmp_path / "rebuilt" / "agentstrata-1.0-py3-none-any.whl"
    built.parent.mkdir()
    rebuilt.parent.mkdir()
    _write_test_wheel(built, verifier, extra_members=built_extra)
    _write_test_wheel(rebuilt, verifier, extra_members=rebuilt_extra)
    verifier.validate_wheel(built)
    verifier.validate_wheel(rebuilt)

    with pytest.raises(verifier.VerificationError, match=difference):
        verifier._compare_wheel_contents(built, rebuilt)


def test_release_wheel_comparison_rejects_content_drift(tmp_path: Path) -> None:
    verifier = _load_verifier()
    built = tmp_path / "built" / "agentstrata-1.0-py3-none-any.whl"
    rebuilt = tmp_path / "rebuilt" / "agentstrata-1.0-py3-none-any.whl"
    built.parent.mkdir()
    rebuilt.parent.mkdir()
    _write_test_wheel(
        built,
        verifier,
        member_overrides={"chatcopilot/__init__.py": b"built\n"},
    )
    _write_test_wheel(
        rebuilt,
        verifier,
        member_overrides={"chatcopilot/__init__.py": b"rebuilt\n"},
    )

    with pytest.raises(verifier.VerificationError, match="content-changed"):
        verifier._compare_wheel_contents(built, rebuilt)


def test_release_wheel_comparison_ignores_zip_timestamps(tmp_path: Path) -> None:
    verifier = _load_verifier()
    built = tmp_path / "built" / "agentstrata-1.0-py3-none-any.whl"
    rebuilt = tmp_path / "rebuilt" / "agentstrata-1.0-py3-none-any.whl"
    built.parent.mkdir()
    rebuilt.parent.mkdir()
    _write_test_wheel(built, verifier, timestamp=(2024, 1, 1, 0, 0, 0))
    _write_test_wheel(rebuilt, verifier, timestamp=(2025, 2, 2, 2, 2, 2))

    verifier._compare_wheel_contents(built, rebuilt)


def test_release_wheel_filename_identity_is_checked() -> None:
    verifier = _load_verifier()
    assert verifier._wheel_filename_identity(
        Path("agentstrata-1.2.3-py3-none-any.whl")
    ) == verifier.ArtifactIdentity(name="agentstrata", version="1.2.3")
    with pytest.raises(verifier.VerificationError, match="project name"):
        verifier._wheel_filename_identity(Path("other-1.2.3-py3-none-any.whl"))


@pytest.mark.parametrize("difference", ("missing", "changed", "duplicate"))
def test_release_wheel_requires_python_must_match_pyproject(
    tmp_path: Path,
    difference: str,
) -> None:
    verifier = _load_verifier()
    wheel = tmp_path / "agentstrata-1.0-py3-none-any.whl"
    metadata = _metadata_payload(verifier)
    source_value = verifier._source_project_data()["project"]["requires-python"]
    line = f"Requires-Python: {source_value}\n".encode()
    assert line in metadata
    if difference == "missing":
        metadata = metadata.replace(line, b"", 1)
    elif difference == "changed":
        metadata = metadata.replace(line, b"Requires-Python: >=3.11,<3.14\n", 1)
    else:
        metadata = metadata.replace(line, line + line, 1)
    _write_test_wheel(wheel, verifier, metadata_payload=metadata)

    with pytest.raises(verifier.VerificationError, match="Requires-Python"):
        verifier.validate_wheel(wheel)


def test_release_wheel_accepts_semantically_equivalent_requires_python(
    tmp_path: Path,
) -> None:
    verifier = _load_verifier()
    wheel = tmp_path / "agentstrata-1.0-py3-none-any.whl"
    metadata = _metadata_payload(verifier)
    source_value = verifier._source_project_data()["project"]["requires-python"]
    equivalent = ",".join(reversed(source_value.split(",")))
    metadata = metadata.replace(
        f"Requires-Python: {source_value}\n".encode(),
        f"Requires-Python: {equivalent}\n".encode(),
        1,
    )
    _write_test_wheel(wheel, verifier, metadata_payload=metadata)

    verifier.validate_wheel(wheel)


def test_release_sdist_requires_python_must_match_pyproject(
    tmp_path: Path,
) -> None:
    verifier = _load_synthetic_sdist_verifier()
    identity = verifier._source_identity()
    root = f"{identity.name}-{identity.version}"
    sdist = tmp_path / f"{root}.tar.gz"
    metadata = _metadata_payload(verifier, version=identity.version)
    source_value = verifier._source_project_data()["project"]["requires-python"]
    metadata = metadata.replace(
        f"Requires-Python: {source_value}\n".encode(),
        b"Requires-Python: >=3.11,<3.14\n",
        1,
    )
    _write_test_sdist(
        sdist,
        verifier,
        extra_members={},
        metadata_payload=metadata,
    )

    with pytest.raises(
        verifier.VerificationError,
        match="sdist PKG-INFO Requires-Python differs",
    ):
        verifier.validate_sdist(sdist)


@pytest.mark.parametrize(
    "markdown",
    (
        "[documentation](https://example.com/docs)",
        "![badge](http://example.com/badge.svg)",
        "[security](mailto:security@example.com)",
        "[section](#section)",
        "[reference]: <https://example.com/reference>",
    ),
)
def test_release_markdown_targets_accept_portable_destinations(
    markdown: str,
) -> None:
    verifier = _load_verifier()

    verifier._validate_markdown_targets(markdown, label="test Markdown")


@pytest.mark.parametrize(
    ("markdown", "target"),
    (
        ("[documentation](docs/README.md)", "docs/README.md"),
        ("![diagram](assets/diagram.svg)", "assets/diagram.svg"),
        ("[reference]: ../README.md", "../README.md"),
        (
            "[![badge](https://example.com/badge.svg)](docs/README.md)",
            "docs/README.md",
        ),
        ("[empty]()", ""),
    ),
)
def test_release_markdown_targets_reject_relative_destinations(
    markdown: str,
    target: str,
) -> None:
    verifier = _load_verifier()

    with pytest.raises(
        verifier.VerificationError,
        match="relative Markdown target",
    ):
        verifier._validate_markdown_targets(markdown, label="test Markdown")


def test_release_source_readme_has_only_portable_markdown_targets() -> None:
    verifier = _load_verifier()

    verifier._validate_markdown_targets(
        (ROOT / "README.md").read_text(encoding="utf-8"),
        label="source README.md",
    )


def test_release_wheel_description_rejects_relative_markdown_target(
    tmp_path: Path,
) -> None:
    verifier = _load_verifier()
    wheel = tmp_path / "agentstrata-1.0-py3-none-any.whl"
    metadata = _metadata_payload(
        verifier,
        description="[documentation](docs/README.md)\n",
    )
    _write_test_wheel(wheel, verifier, metadata_payload=metadata)

    with pytest.raises(
        verifier.VerificationError,
        match="wheel METADATA Description contains relative Markdown target",
    ):
        verifier.validate_wheel(wheel)


def test_release_sdist_description_rejects_relative_markdown_target(
    tmp_path: Path,
) -> None:
    verifier = _load_synthetic_sdist_verifier()
    identity = verifier._source_identity()
    root = f"{identity.name}-{identity.version}"
    sdist = tmp_path / f"{root}.tar.gz"
    metadata = _metadata_payload(
        verifier,
        version=identity.version,
        description="![diagram](assets/diagram.svg)\n",
    )
    _write_test_sdist(
        sdist,
        verifier,
        extra_members={},
        metadata_payload=metadata,
    )

    with pytest.raises(
        verifier.VerificationError,
        match="sdist PKG-INFO Description contains relative Markdown target",
    ):
        verifier.validate_sdist(sdist)
