"""Production host dependency declarations stay bound to frozen source inputs."""
from __future__ import annotations

import json
import subprocess

import pytest

from chatcopilot.deployment import windows_dependencies as dependencies


@pytest.fixture
def inputs(tmp_path):
    source = tmp_path / "source"
    data = {
        "pyproject.toml": b'[project]\nname="agentstrata"\nversion="0.1"\nrequires-python=">=3.12,<3.14"\ndependencies=["base>=1"]\n[project.optional-dependencies]\nagent=["worker==2"]\n',
        "uv.lock": b'[[package]]\nname="agentstrata"\nsource={editable="."}\n[[package]]\nname="base"\nversion="1.0"\nsource={registry="https://pypi.org/simple"}\n',
        dependencies.HOST_LOCK: b'# ignored annotation\nbase==1.0 \\\n    --hash=sha256:' + b'a' * 64 + b'\nworker==2 \\\n    --hash=sha256:' + b'b' * 64 + b'\n',
        dependencies.RUNTIME_LOCK: b'{"schema":"agentstrata-windows-runtime-inputs/v1","host_python":"3.13.15"}',
    }
    manifest = {}
    for name, raw in data.items():
        target = source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        manifest[name] = {"sha256": dependencies._sha(raw), "executable": False}
    return source, data, manifest


def _mock_export(monkeypatch):
    monkeypatch.setattr(dependencies, "_export", lambda data, _uv: dependencies.canonical_requirements(data[dependencies.HOST_LOCK]))


def test_complete_core_and_fixed_agent_roots_bind_source_and_remain_declarations(inputs, monkeypatch):
    source, data, manifest = inputs
    _mock_export(monkeypatch)
    raw = dependencies.build_dependency_declarations(source, manifest)
    value = json.loads(raw)
    assert value["requirements"] == ["base>=1", "worker==2"]
    assert value["extras"] == ["agent"]
    assert value["target"]["python_version"] == "3.13.15"
    assert value["inputs"] == {name: dependencies._sha(content) for name, content in data.items()}
    assert value["source_digest"] == dependencies.manifest_digest(manifest)
    assert value["state"] == "declared"
    assert not any(value[field] for field in ("wheel_closure_verified", "native_abi_verified", "installation_accepted"))
    dependencies.verify_dependency_declarations(source, manifest, raw)


@pytest.mark.parametrize("name", tuple(dependencies.INPUT_LIMITS))
def test_every_input_must_match_independently_frozen_source(inputs, monkeypatch, name):
    source, _data, manifest = inputs
    _mock_export(monkeypatch)
    (source / name).write_bytes((source / name).read_bytes() + b"\n")
    with pytest.raises(ValueError, match="frozen input"):
        dependencies.build_dependency_declarations(source, manifest)


@pytest.mark.parametrize("name", tuple(dependencies.INPUT_LIMITS))
def test_unrecorded_input_cannot_supply_its_own_provenance(inputs, monkeypatch, name):
    source, _data, manifest = inputs
    _mock_export(monkeypatch)
    del manifest[name]
    with pytest.raises(ValueError, match="lacks"):
        dependencies.build_dependency_declarations(source, manifest)


@pytest.mark.parametrize("mutation", ["roots", "source", "target", "abi", "accepted", "extra-key", "noncanonical", "duplicate", "deep-json"])
def test_rebinding_artifact_digest_cannot_change_source_derived_contract(inputs, monkeypatch, mutation):
    source, _data, manifest = inputs
    _mock_export(monkeypatch)
    value = json.loads(dependencies.build_dependency_declarations(source, manifest))
    if mutation == "roots":
        value["requirements"] = ["base>=1"]
    elif mutation == "source":
        value["source_digest"] = "1" * 64
    elif mutation == "target":
        value["target"]["python_version"] = "3.12.14"
    elif mutation == "abi":
        value["native_abi_verified"] = True
    elif mutation == "accepted":
        value["installation_accepted"] = 0
    elif mutation == "extra-key":
        value["ready"] = True
    raw = dependencies.canonical(value)
    if mutation == "noncanonical":
        raw += b" "
    elif mutation == "duplicate":
        raw = b'{"state":"accepted",' + raw[1:]
    elif mutation == "deep-json":
        raw = b"[" * 5000 + b"]" * 5000
    with pytest.raises(ValueError, match="differ"):
        dependencies.verify_dependency_declarations(source, manifest, raw)


@pytest.mark.parametrize("change", ["url-root", "workspace", "dynamic", "python", "missing-agent", "external-source", "registry-secret", "duplicate-runtime"])
def test_unsupported_declarations_are_rejected_before_compiler(inputs, monkeypatch, change):
    source, data, manifest = inputs
    if change == "url-root":
        data["pyproject.toml"] = data["pyproject.toml"].replace(b"base>=1", b"base @ https://private.invalid/token")
    elif change == "workspace":
        data["pyproject.toml"] += b'\n[tool.uv.workspace]\nmembers=["../../outside"]\n'
    elif change == "dynamic":
        data["pyproject.toml"] = data["pyproject.toml"].replace(b'version="0.1"', b'version="0.1"\ndynamic=["dependencies"]')
    elif change == "python":
        data[dependencies.RUNTIME_LOCK] = data[dependencies.RUNTIME_LOCK].replace(b"3.13.15", b"3.14.0")
    elif change == "missing-agent":
        data["pyproject.toml"] = data["pyproject.toml"].replace(b'agent=["worker==2"]', b'agent=[]')
    elif change == "external-source":
        data["uv.lock"] = data["uv.lock"].replace(b'registry="https://pypi.org/simple"', b'path="../../outside"')
    elif change == "registry-secret":
        userinfo = b":".join((b"fixture-user", b"fixture-secret"))
        registry = b"https://" + userinfo + b"@index.invalid/simple"
        data["uv.lock"] = data["uv.lock"].replace(b"https://pypi.org/simple", registry)
    elif change == "duplicate-runtime":
        data[dependencies.RUNTIME_LOCK] = data[dependencies.RUNTIME_LOCK].replace(b'{', b'{"host_python":"3.12.14",', 1)
    for name, raw in data.items():
        (source / name).write_bytes(raw)
        manifest[name]["sha256"] = dependencies._sha(raw)
    monkeypatch.setattr(dependencies, "_export", lambda *_: pytest.fail("Invalid declarations reached the host compiler"))
    with pytest.raises(ValueError) as error:
        dependencies.build_dependency_declarations(source, manifest)
    assert "fixture-secret" not in str(error.value)


def test_production_build_refuses_drift_without_rewriting_inputs(inputs, monkeypatch):
    source, data, manifest = inputs
    monkeypatch.setattr(dependencies, "_export", lambda *_: b"different frozen requirements\n")
    with pytest.raises(ValueError, match="regenerate"):
        dependencies.build_dependency_declarations(source, manifest)
    assert all((source / name).read_bytes() == raw for name, raw in data.items())


def test_regeneration_uses_frozen_project_without_requiring_old_host_lock(inputs, monkeypatch):
    source, data, _manifest = inputs
    expected = dependencies.canonical_requirements(data[dependencies.HOST_LOCK])
    (source / dependencies.HOST_LOCK).unlink()

    def export(frozen, _uv):
        assert dependencies.HOST_LOCK not in frozen
        assert set(frozen) == set(dependencies.INPUT_LIMITS) - {dependencies.HOST_LOCK}
        return expected

    monkeypatch.setattr(dependencies, "_export", export)
    assert dependencies.checked_host_requirements(source) == expected
    assert not (source / dependencies.HOST_LOCK).exists()


@pytest.mark.parametrize("bad", [b"base>=1", b"base @ https://private.invalid/token", b"--index-url https://private.invalid", b"-e ."])
def test_lock_parser_rejects_unpinned_or_option_requirements(bad):
    with pytest.raises(ValueError):
        dependencies.canonical_requirements(bad + b" \\\n    --hash=sha256:" + b"a" * 64 + b"\n")


def test_compiler_uses_only_frozen_files_and_no_parent_secrets(inputs, monkeypatch, tmp_path):
    _source, data, _manifest = inputs
    uv = tmp_path / "host-uv"
    uv.write_bytes(b"not executed: subprocess is controlled")
    monkeypatch.setenv("UV_FROZEN", "1")
    monkeypatch.setenv("UV_EXCLUDE", "private-path")
    monkeypatch.setenv("UV_NO_HASHES", "true")
    monkeypatch.setenv("PYTHONPATH", "untrusted-candidate")
    monkeypatch.setenv("HTTPS_PROXY", "fixture-secret")
    calls = []

    def run(argv, **options):
        calls.append(argv)
        assert set(path.name for path in options["cwd"].iterdir()) == {"pyproject.toml", "uv.lock"}
        assert options["env"].keys().isdisjoint({"UV_FROZEN", "UV_EXCLUDE", "UV_NO_HASHES", "PYTHONPATH", "HTTPS_PROXY"})
        assert options["timeout"] == 30 and options["check"] is True
        if argv[-1] == "--version":
            return subprocess.CompletedProcess(argv, 0, b"uv 0.12.5 (host)\n", b"")
        for flag in ("--locked", "--offline", "--no-build", "--no-config", "--no-cache", "--no-python-downloads", "--no-default-groups"):
            assert flag in argv
        assert argv[argv.index("--extra") + 1] == "agent"
        return subprocess.CompletedProcess(argv, 0, data[dependencies.HOST_LOCK], b"")

    monkeypatch.setattr(dependencies.subprocess, "run", run)
    assert dependencies._export(data, uv) == dependencies.canonical_requirements(data[dependencies.HOST_LOCK])
    assert len(calls) == 2


@pytest.mark.parametrize("failure", ["version", "lock-changed", "failure", "timeout", "oversized"])
def test_compiler_failure_cannot_issue_a_declaration(inputs, monkeypatch, tmp_path, failure):
    _source, data, _manifest = inputs
    uv = tmp_path / "host-uv"
    uv.touch()

    def run(argv, **options):
        if argv[-1] == "--version":
            return subprocess.CompletedProcess(argv, 0, b"uv 0.12.4\n" if failure == "version" else b"uv 0.12.5\n", b"")
        if failure == "lock-changed":
            (options["cwd"] / "uv.lock").write_bytes(b"rewritten")
        elif failure == "failure":
            raise subprocess.CalledProcessError(1, argv, stderr=b"fixture-secret")
        elif failure == "timeout":
            raise subprocess.TimeoutExpired(argv, 30, stderr=b"fixture-secret")
        raw = b"x" * (dependencies.INPUT_LIMITS[dependencies.HOST_LOCK] + 1) if failure == "oversized" else data[dependencies.HOST_LOCK]
        return subprocess.CompletedProcess(argv, 0, raw, b"")

    monkeypatch.setattr(dependencies.subprocess, "run", run)
    with pytest.raises(ValueError) as error:
        dependencies._export(data, uv)
    assert "fixture-secret" not in str(error.value)
